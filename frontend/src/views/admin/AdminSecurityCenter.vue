<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { Aim, CircleCheck, Clock, DataAnalysis, Lock, Refresh, Search, WarningFilled } from '@element-plus/icons-vue'
import { type SecurityDecoyStatus, applyDecoyRedirect, getDecoyStatus, getDefenseSurface, getSecurityCenterEvents, getSecurityCenterOverview, getTrafficSummary, runSecurityMonitor, traceSecurityIp, type AutomaticBlockingSnapshot, type SecurityCenterEvent, type SecurityCenterEventGroup, type SecurityCenterEventPage, type SecurityCenterOverview, type SecurityIpTrace, type SecurityMonitorPolicy, type SecuritySurfaceAudit, type SecurityTrafficPeer, type SecurityTrafficSummary, updateSecurityMonitorPolicy } from '@/api/adminSecurityCenter'
import { getSystemStatus, type SystemStatus } from '@/api/adminOverview'
import { resolveAlert } from '@/api/adminGovernance'
import AutomaticBlockingPanel from '@/components/security/AutomaticBlockingPanel.vue'

const overview = ref<SecurityCenterOverview | null>(null)
const eventPage = ref<SecurityCenterEventPage | null>(null)
const recentPage = ref<SecurityCenterEventPage | null>(null)
const recentLoading = ref(true)
const recentError = ref('')
const eventsInitialized = ref(false)
const sections = [
  { id: 'overview', label: '安全概览' },
  { id: 'events', label: '事件记录' },
  { id: 'strategies', label: '防御策略' },
  { id: 'trace', label: '溯源与响应' },
  { id: 'decoy', label: '诱捕层' },
] as const
type SecuritySection = typeof sections[number]['id']
const activeSection = ref<SecuritySection>('overview')
const eventGroup = ref<SecurityCenterEventGroup>('activity')
const systemStatus = ref<SystemStatus | null>(null)
const overviewLoading = ref(true)
const eventsLoading = ref(false)
const serverLoading = ref(true)
const overviewError = ref('')
const eventsError = ref('')
const serverError = ref('')
const runLoading = ref(false)
const saveLoading = ref(false)
const policyFeedback = ref('')
const policyFeedbackError = ref(false)
const resolvingAlertId = ref<number | null>(null)
const hours = ref(24)
const page = ref(1)
const clock = ref(Date.now())
const blockingPanel = ref<InstanceType<typeof AutomaticBlockingPanel> | null>(null)
const blockingStatus = reactive<{ snapshot: AutomaticBlockingSnapshot | null; loading: boolean; error: string }>({ snapshot: null, loading: true, error: '' })
const traceInitialized = ref(false)
const traceIp = ref('')
const traceQueriedIp = ref('')
const traceInlineError = ref('')
const traceLoading = ref(false)
const traceError = ref('')
const traceResult = ref<SecurityIpTrace | null>(null)
const surface = ref<SecuritySurfaceAudit | null>(null)
const surfaceLoading = ref(false)
const surfaceError = ref('')
const publicListenersOnly = ref(false)
const trafficHours = ref(24)
const traffic = ref<SecurityTrafficSummary | null>(null)
const trafficLoading = ref(false)
const trafficError = ref('')
let eventRequestGeneration = 0
let recentRequestGeneration = 0
let overviewRequestGeneration = 0
let serverRequestGeneration = 0
let traceRequestGeneration = 0
let surfaceRequestGeneration = 0
let trafficRequestGeneration = 0
let clockTimer: ReturnType<typeof setInterval> | undefined
const policyDraft = reactive({
  ssh_failed_threshold: 20,
  ssh_window_hours: 1,
  nginx_failure_threshold: 20,
  nginx_window_hours: 1,
})

const policyFields = ['ssh_failed_threshold', 'ssh_window_hours', 'nginx_failure_threshold', 'nginx_window_hours'] as const
const policyDirty = computed(() => Boolean(overview.value && policyFields.some((key) => policyDraft[key] !== overview.value?.policy[key])))
const refreshing = computed(() => overviewLoading.value || eventsLoading.value || recentLoading.value || serverLoading.value || blockingStatus.loading)
const recentEvents = computed(() => recentPage.value?.items.slice(0, 5) ?? [])
const resourceMetrics = [
  { key: 'cpu_percent', id: 'cpu', label: 'CPU 使用率' },
  { key: 'memory_percent', id: 'memory', label: '内存使用率' },
  { key: 'disk_percent', id: 'disk', label: '磁盘使用率' },
] as const

function selectSection(section: SecuritySection): void {
  activeSection.value = section
  if (section === 'events' && !eventsInitialized.value) void loadEvents()
  if (section === 'trace') initializeTracePanel()
}

function handleTabKey(event: KeyboardEvent, index: number): void {
  let next: number
  if (event.key === 'ArrowRight') next = (index + 1) % sections.length
  else if (event.key === 'ArrowLeft') next = (index - 1 + sections.length) % sections.length
  else if (event.key === 'Home') next = 0
  else if (event.key === 'End') next = sections.length - 1
  else return
  event.preventDefault()
  selectSection(sections[next].id)
  const tabList = (event.currentTarget as HTMLElement).parentElement
  tabList?.querySelectorAll<HTMLButtonElement>('[role=tab]')[next]?.focus()
}

async function viewAllEvents(): Promise<void> {
  if (eventGroup.value !== 'activity' || hours.value !== 24 || page.value !== 1) {
    eventGroup.value = 'activity'
    hours.value = 24
    page.value = 1
    void loadEvents()
  }
  selectSection('events')
  await nextTick()
  document.getElementById('security-tab-events')?.focus()
}

async function viewInspectionEvents(): Promise<void> {
  eventGroup.value = 'inspection'
  hours.value = 24
  page.value = 1
  void loadEvents()
  selectSection('events')
  await nextTick()
  document.getElementById('security-tab-events')?.focus()
}

function resourceValue(key: typeof resourceMetrics[number]['key']): number | null {
  if (serverLoading.value || serverError.value || !systemStatus.value?.available) return null
  const value = systemStatus.value[key]
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}

function resourceLabel(key: typeof resourceMetrics[number]['key']): string {
  if (serverLoading.value) return '读取中…'
  if (serverError.value) return '状态未知'
  if (!systemStatus.value?.available) return '不可用'
  const value = resourceValue(key)
  return value === null ? '未知' : `${Number(value.toFixed(1))}%`
}

const uptimeLabel = computed(() => {
  if (serverLoading.value) return '读取中…'
  if (serverError.value || !systemStatus.value?.available) return '状态未知'
  const seconds = systemStatus.value.uptime_seconds
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '未知'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分钟`
  const hours = Math.floor(minutes / 60)
  return hours < 24 ? `${hours} 小时 ${minutes % 60} 分钟` : `${Math.floor(hours / 24)} 天 ${hours % 24} 小时`
})

function sourceStatusLabel(status: string): string {
  if (overviewError.value) return '状态未知'
  if (monitorRunIsStale()) return '已过期'
  return ({ success: '采集成功', failed: '采集失败', degraded: '采集降级', unknown: '状态未知' } as Record<string, string>)[status] || '状态未知'
}

function sourceStatusTone(status: string): string {
  if (overviewError.value || status === 'unknown') return 'unknown'
  return status === 'success' && !monitorRunIsStale() ? 'healthy' : 'warning'
}

function monitorRunIsStale(): boolean {
  const run = overview.value?.monitoring.last_run
  const referenceAt = run?.finished_at || (run?.status === 'running' ? run.started_at : null)
  if (!referenceAt) return false
  const timestamp = Date.parse(referenceAt)
  if (!Number.isFinite(timestamp)) return true
  const intervalMinutes = overview.value?.monitoring.interval_minutes ?? 0
  const staleAfterMinutes = Math.max(intervalMinutes * 3, 15)
  return clock.value - timestamp > staleAfterMinutes * 60_000
}

const lastRunLabel = computed(() => {
  if (overviewError.value) return '状态未知'
  const run = overview.value?.monitoring.last_run
  if (!run || run.status === 'unknown') return '尚无巡检记录'
  if (run.status === 'success' && !run.finished_at) return '巡检记录不完整'
  const status = run.status === 'running'
    ? monitorRunIsStale() ? '巡检超过预期' : '巡检进行中'
    : run.status !== 'success' || run.degraded || run.failed_sources > 0
      ? '最近巡检异常'
      : monitorRunIsStale()
        ? '最近巡检已过期'
        : '最近巡检完成'
  return run.finished_at ? `${status} · ${formatTime(run.finished_at)}` : status
})
const coverageLabel = computed(() => {
  if (overviewError.value) return '数据源状态未知'
  const sources = overview.value?.monitoring.sources ?? []
  if (!sources.length) return '数据源状态未知'
  if (monitorRunIsStale()) return '最近采集结果已过期'
  const unknown = sources.filter((source) => source.status === 'unknown').length
  const failed = sources.filter((source) => source.status === 'failed').length
  const degraded = sources.filter((source) => source.status === 'degraded').length
  if (unknown === sources.length) return '尚未取得数据源回执'
  if (failed && degraded) return `${failed + degraded} 个来源失败或降级`
  if (failed) return `${failed} 个数据源异常`
  if (degraded) return `${degraded} 个数据源降级`
  return unknown ? `${unknown} 个数据源状态未知` : `${sources.length} 个数据源最近成功`
})
const monitoringTone = computed(() => {
  if (overviewError.value || !overview.value) return 'unknown'
  const { enabled, last_run: run } = overview.value.monitoring
  if (!enabled) return 'warning'
  if (run.status === 'running') return monitorRunIsStale() ? 'warning' : 'unknown'
  if (run.status !== 'unknown' && (
    run.status !== 'success' || run.failed_sources > 0 || run.degraded || monitorRunIsStale()
  )) return 'warning'
  if (run.status === 'unknown' || !run.finished_at) return 'unknown'
  return 'healthy'
})
const monitoringLabel = computed(() => {
  if (overviewError.value || !overview.value) return '状态未知'
  const run = overview.value.monitoring.last_run
  if (!overview.value.monitoring.enabled) return '巡检计划未启用'
  if (run.status === 'running') return monitorRunIsStale() ? '巡检超过预期' : '巡检进行中'
  if (run.status === 'success' && !run.finished_at) return '巡检记录不完整'
  if (run.status === 'unknown' || !run.finished_at) return '尚无有效巡检'
  if (run.status !== 'success' || run.failed_sources > 0 || run.degraded) return '巡检存在异常'
  if (monitorRunIsStale()) return '巡检记录已过期'
  return '近期巡检正常'
})
const popupSeverityLabel = computed(() => ({
  info: '信息及以上', warning: '警告及以上', high: '高风险及以上', critical: '仅危急',
} as Record<string, string>)[overview.value?.policy.popup_min_severity || 'warning'])
const blockingLabel = computed(() => {
  if (blockingStatus.loading || blockingStatus.error || !blockingStatus.snapshot) return '自动封禁：状态待核验'
  if (!blockingStatus.snapshot.available) return '自动封禁：执行器不可用'
  if (!blockingStatus.snapshot.verified || blockingStatus.snapshot.outcome_unknown) return '自动封禁：执行状态待核验'
  return blockingStatus.snapshot.enabled ? '自动封禁：已启用' : '自动封禁：关闭'
})
const securityModeLabel = computed(() => blockingStatus.snapshot?.available && blockingStatus.snapshot.verified && blockingStatus.snapshot.enabled
  && !blockingStatus.snapshot.outcome_unknown && !blockingStatus.loading && !blockingStatus.error ? '监控与临时自动封禁' : '监控与告警')

function getErrorMessage(error: unknown): string {
  const value = error as { message?: string; response?: { data?: { message?: string } } } | null
  return value?.response?.data?.message || value?.message || '请求失败，请稍后重试。'
}

function formatTime(value?: string | null): string {
  if (!value) return '未知'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间格式异常' : date.toLocaleString('zh-CN', { hour12: false })
}

function eventStatusLabel(value: string): string {
  const labels: Record<string, string> = {
    success: '成功', failed: '失败', running: '进行中', open: '待处理', resolved: '已处理',
    warning: '部分异常', unknown: '状态未知', blocked: '已封禁', expired: '已到期', released: '已解封',
    active: '生效中',
  }
  return labels[value] || value
}

function evidenceEntries(event: SecurityCenterEvent): Array<[string, string]> {
  const labels: Record<string, string> = {
    ip: '来源 IP', kind: '类型', failed_count: '失败次数', failure_count: '异常次数',
    scanner_count: '探测次数', threshold: '告警阈值', window_hours: '观察窗口（小时）',
    source_truncated: '采集是否截断', status_counts: 'HTTP 状态计数',
    risk_level: '风险级别', duration_ms: '耗时（毫秒）',
    revision: '策略版本', source: '触发来源',
    action_code: '审计动作', target_type: '目标类型', account: '尝试账号',
    completed_sources: '完成来源数', failed_sources: '失败来源数', degraded_sources: '降级来源数',
    failed_source_codes: '失败来源', degraded_source_codes: '降级来源', confirmed: '核验结果', verified: '回执核验',
    enabled: '启用状态', automatic_blocking_enabled: '自动封禁', counterattack_enabled: '反击操作',
    routine_check: '例行核验', recent_blocks: '近期封禁记录数', active_blocks: '当前封禁记录数',
    request_id: '执行记录编号', ai_anomaly_enabled: '小菱异常研判',
  }
  return Object.entries(event.evidence_summary || {}).map(([key, value]) => [
    labels[key] || key,
    typeof value === 'boolean' ? evidenceBooleanLabel(key, value)
      : value === null || value === undefined ? '未记录'
        : typeof value === 'object' ? JSON.stringify(value) : String(value),
  ])
}

function evidenceBooleanLabel(key: string, value: boolean): string {
  if (['enabled', 'automatic_blocking_enabled', 'counterattack_enabled', 'ai_anomaly_enabled'].includes(key)) return value ? '已启用' : '已关闭'
  if (key === 'source_truncated') return value ? '已截断' : '未截断'
  if (key === 'routine_check') return value ? '是' : '否'
  return value ? '已确认' : '未确认'
}

function syncPolicy(policy: SecurityMonitorPolicy): void {
  policyDraft.ssh_failed_threshold = policy.ssh_failed_threshold
  policyDraft.ssh_window_hours = policy.ssh_window_hours
  policyDraft.nginx_failure_threshold = policy.nginx_failure_threshold
  policyDraft.nginx_window_hours = policy.nginx_window_hours
}

function resetPolicy(): void {
  if (!overview.value || overviewError.value || saveLoading.value) return
  syncPolicy(overview.value.policy)
  policyFeedback.value = ''
}

async function loadOverview(): Promise<void> {
  const requestGeneration = ++overviewRequestGeneration
  overviewLoading.value = true
  try {
    const result = await getSecurityCenterOverview()
    if (requestGeneration !== overviewRequestGeneration) return
    const preserveDraft = policyDirty.value || saveLoading.value
    overview.value = result
    if (!preserveDraft) syncPolicy(result.policy)
    overviewError.value = ''
  } catch (error) {
    if (requestGeneration === overviewRequestGeneration) overviewError.value = getErrorMessage(error)
  } finally {
    if (requestGeneration === overviewRequestGeneration) overviewLoading.value = false
  }
}

async function loadRecentEvents(): Promise<void> {
  const requestGeneration = ++recentRequestGeneration
  recentLoading.value = true
  recentError.value = ''
  recentPage.value = null
  try {
    const result = await getSecurityCenterEvents(24, 1, 5, 'activity')
    if (requestGeneration === recentRequestGeneration) recentPage.value = result
  } catch (error) {
    if (requestGeneration === recentRequestGeneration) recentError.value = getErrorMessage(error)
  } finally {
    if (requestGeneration === recentRequestGeneration) recentLoading.value = false
  }
}

async function loadEvents(): Promise<void> {
  eventsInitialized.value = true
  const requestGeneration = ++eventRequestGeneration
  const requestedHours = hours.value
  const requestedPage = page.value
  const requestedGroup = eventGroup.value
  eventsLoading.value = true
  eventsError.value = ''
  eventPage.value = null
  try {
    const result = await getSecurityCenterEvents(requestedHours, requestedPage, 20, requestedGroup)
    if (requestGeneration !== eventRequestGeneration) return
    const lastPage = Math.max(result.pages, 1)
    if (requestedPage > lastPage) {
      page.value = lastPage
      await loadEvents()
      return
    }
    page.value = result.page
    eventPage.value = result
  } catch (error) {
    if (requestGeneration === eventRequestGeneration) eventsError.value = getErrorMessage(error)
  } finally {
    if (requestGeneration === eventRequestGeneration) eventsLoading.value = false
  }
}

async function loadServer(): Promise<void> {
  const requestGeneration = ++serverRequestGeneration
  serverLoading.value = true
  try {
    const result = await getSystemStatus()
    if (requestGeneration === serverRequestGeneration) {
      systemStatus.value = result
      serverError.value = ''
    }
  } catch (error) {
    if (requestGeneration === serverRequestGeneration) serverError.value = getErrorMessage(error)
  } finally {
    if (requestGeneration === serverRequestGeneration) serverLoading.value = false
  }
}

/* ---------- 溯源与响应：全部只读，不向目标发包 ---------- */

const firewallFamilies = [{ key: 'ipv4', label: 'IPv4' }, { key: 'ipv6', label: 'IPv6' }] as const
const riskLevelLabels: Record<string, string> = { low: '低风险', medium: '中风险', high: '高风险', critical: '危急风险' }

function riskLevelLabel(level: string): string {
  return riskLevelLabels[level] || (level ? `风险等级 ${level}` : '风险等级未知')
}

function riskLevelTone(level: string): string {
  return ({ low: 'low', medium: 'warning', high: 'high', critical: 'critical' } as Record<string, string>)[level] || 'unknown'
}

/** 取回执中的首条有效说明，用于不可用时的错误条。 */
function firstIssue(errors: string[] | undefined): string {
  return (errors || []).find((item) => typeof item === 'string' && item.trim())?.trim() || ''
}

function isPublicListener(listener: { address: string }): boolean {
  return ['0.0.0.0', '::', '*'].includes(listener.address.trim())
}

function isIpv4Address(value: string): boolean {
  const parts = value.split('.')
  return parts.length === 4
    && parts.every((part) => /^\d{1,3}$/.test(part) && (part === '0' || !part.startsWith('0')) && Number(part) <= 255)
}

function isIpv6Address(value: string): boolean {
  if (!value.includes(':') || !/^[0-9a-fA-F:.]+$/.test(value)) return false
  const blocks = value.split('::')
  if (blocks.length > 2) return false
  const head = blocks[0] ? blocks[0].split(':') : []
  const tail = blocks.length === 2 && blocks[1] ? blocks[1].split(':') : []
  const groups = [...head, ...tail]
  if (!groups.length) return value.includes('::')
  const literalGroups = groups.filter((group) => group.includes('.'))
  if (literalGroups.length > 1) return false
  if (literalGroups.length === 1 && !isIpv4Address(groups[groups.length - 1])) return false
  if (groups.some((group) => !group.includes('.') && !/^[0-9a-fA-F]{1,4}$/.test(group))) return false
  const groupCount = groups.reduce((total, group) => total + (group.includes('.') ? 2 : 1), 0)
  return value.includes('::') ? groupCount <= 7 : groupCount === 8
}

/** 校验单个 IP；返回空串表示可提交，否则返回行内提示。 */
function traceIpValidation(value: string): string {
  if (!value) return '请输入要溯源的来源 IP。'
  if (value.includes('/')) return '只接受单个 IP，请输入具体地址而不是网段。'
  if (isIpv4Address(value) || isIpv6Address(value)) return ''
  return 'IP 格式不正确，请输入单个 IPv4 或 IPv6 地址。'
}

function countEntries(counts: Record<string, number> | undefined, limit: number): Array<[string, number]> {
  return Object.entries(counts || {})
    .filter(([, count]) => typeof count === 'number')
    .sort((left, right) => right[1] - left[1])
    .slice(0, limit)
}

function peerEndpointSummary(peer: SecurityTrafficPeer): string {
  const ports = countEntries(peer.peer_ports, 3).map(([port, count]) => `${port} × ${count}`)
  const protocols = countEntries(peer.protocols, 2).map(([name, count]) => `${name} × ${count}`)
  return [...(ports.length ? [`端口 ${ports.join('、')}`] : []), ...(protocols.length ? [`协议 ${protocols.join('、')}`] : [])].join(' · ') || '未记录'
}

function peerProcessSummary(peer: SecurityTrafficPeer): string {
  const states = countEntries(peer.states, 3).map(([state, count]) => `${state} × ${count}`)
  return [...(peer.processes || []), ...(states.length ? [`状态 ${states.join('、')}`] : [])].join(' · ')
}

const visibleListeners = computed(() => {
  const listeners = surface.value?.listeners || []
  return publicListenersOnly.value ? listeners.filter(isPublicListener) : listeners
})

function initializeTracePanel(): void {
  if (traceInitialized.value) return
  traceInitialized.value = true
  void Promise.allSettled([loadSurface(), loadTraffic()])
}

async function submitTrace(): Promise<void> {
  if (traceLoading.value) return
  const target = traceIp.value.trim()
  const invalid = traceIpValidation(target)
  if (invalid) {
    traceInlineError.value = invalid
    return
  }
  traceInlineError.value = ''
  traceQueriedIp.value = target
  const requestGeneration = ++traceRequestGeneration
  traceLoading.value = true
  traceError.value = ''
  traceResult.value = null
  try {
    const result = await traceSecurityIp(target)
    if (requestGeneration !== traceRequestGeneration) return
    if (result.available !== true || result.verified !== true) {
      traceError.value = `溯源不可用：${firstIssue(result.errors) || '宿主机未返回可用回执，请稍后重试。'}`
      return
    }
    traceResult.value = result
  } catch (error) {
    if (requestGeneration === traceRequestGeneration) traceError.value = `溯源读取失败：${getErrorMessage(error)}`
  } finally {
    if (requestGeneration === traceRequestGeneration) traceLoading.value = false
  }
}

async function retryTrace(): Promise<void> {
  if (traceLoading.value) return
  const target = traceQueriedIp.value || traceIp.value.trim()
  if (traceIpValidation(target)) return
  traceIp.value = target
  await submitTrace()
}

async function loadSurface(): Promise<void> {
  const requestGeneration = ++surfaceRequestGeneration
  surfaceLoading.value = true
  surfaceError.value = ''
  try {
    const result = await getDefenseSurface()
    if (requestGeneration !== surfaceRequestGeneration) return
    if (result.available !== true || result.verified !== true) {
      surface.value = null
      surfaceError.value = `防御面不可用：${firstIssue(result.errors) || '宿主机未返回可用回执，请稍后重试。'}`
      return
    }
    surface.value = result
  } catch (error) {
    if (requestGeneration === surfaceRequestGeneration) {
      surface.value = null
      surfaceError.value = `防御面读取失败：${getErrorMessage(error)}`
    }
  } finally {
    if (requestGeneration === surfaceRequestGeneration) surfaceLoading.value = false
  }
}

async function loadTraffic(): Promise<void> {
  const requestGeneration = ++trafficRequestGeneration
  const requestedHours = trafficHours.value
  trafficLoading.value = true
  trafficError.value = ''
  try {
    const result = await getTrafficSummary(requestedHours)
    if (requestGeneration !== trafficRequestGeneration) return
    if (result.available !== true || result.verified !== true) {
      traffic.value = null
      trafficError.value = `流量元数据不可用：${firstIssue(result.errors) || '宿主机未返回可用回执，请稍后重试。'}`
      return
    }
    traffic.value = result
  } catch (error) {
    if (requestGeneration === trafficRequestGeneration) {
      traffic.value = null
      trafficError.value = `流量元数据读取失败：${getErrorMessage(error)}`
    }
  } finally {
    if (requestGeneration === trafficRequestGeneration) trafficLoading.value = false
  }
}

async function refreshAll(refreshBlocking = true): Promise<void> {
  await Promise.allSettled([
    loadOverview(), loadRecentEvents(), loadServer(),
    ...(eventsInitialized.value ? [loadEvents()] : []),
    ...(traceInitialized.value ? [loadSurface(), loadTraffic()] : []),
    ...(refreshBlocking && blockingPanel.value ? [blockingPanel.value.refresh()] : []),
  ])
}

async function refreshEvents(): Promise<void> {
  page.value = 1
  await loadEvents()
}

async function refreshRecordedEvents(): Promise<void> {
  await Promise.allSettled([loadRecentEvents(), ...(eventsInitialized.value ? [loadEvents()] : [])])
}

async function triggerMonitor(): Promise<void> {
  if (runLoading.value) return
  runLoading.value = true
  try {
    const result = await runSecurityMonitor()
    if (result.success) ElMessage.success(`巡检完成，新增 ${result.created_alerts.length} 条告警。`)
    else ElMessage.warning(`巡检存在数据源异常（${result.errors.length} 项），请查看数据源状态。`)
    await refreshAll()
  } catch (error) {
    ElMessage.error(`巡检未完成：${getErrorMessage(error)}`)
  } finally {
    runLoading.value = false
  }
}

async function savePolicy(): Promise<void> {
  if (!overview.value || overviewError.value || overviewLoading.value || saveLoading.value || !policyDirty.value) return
  saveLoading.value = true
  policyFeedback.value = ''
  policyFeedbackError.value = false
  const submitted = { ...policyDraft }
  try {
    const updated = await updateSecurityMonitorPolicy(submitted)
    // 保存结果比已在途的旧概览更新，避免旧请求覆盖已保存配置。
    ++overviewRequestGeneration
    overviewLoading.value = false
    overview.value.policy = updated
    syncPolicy(updated)
    policyFeedback.value = '已保存，变更已记录审计。'
    ElMessage.success('监控灵敏度已更新，并写入操作审计。')
    await refreshRecordedEvents()
  } catch (error) {
    policyFeedbackError.value = true
    policyFeedback.value = `策略未保存：${getErrorMessage(error)}`
    ElMessage.error(policyFeedback.value)
  } finally {
    saveLoading.value = false
  }
}

async function changePage(next: number): Promise<void> {
  if (eventsLoading.value || next < 1 || next > Math.max(eventPage.value?.pages ?? 1, 1)) return
  page.value = next
  await loadEvents()
}

async function resolveSecurityAlert(event: SecurityCenterEvent): Promise<void> {
  if (!event.alert_id || resolvingAlertId.value !== null) return
  try {
    const answer = await ElMessageBox.prompt('处理说明会作为告警记录的补充信息保存，原始证据会保留。', '标记告警已处理', {
      confirmButtonText: '保存处理记录',
      cancelButtonText: '取消',
      inputType: 'textarea',
      inputPlaceholder: '说明核验结论或后续处理情况',
      inputValidator: (value: string) => value.length <= 2000 || '最多输入 2000 个字符',
    })
    resolvingAlertId.value = event.alert_id
    await resolveAlert(event.alert_id, answer.value || '管理员确认处理')
    ElMessage.success('告警已处理，原始证据已保留。')
    await Promise.allSettled([loadOverview(), refreshRecordedEvents()])
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(`处理记录未保存：${getErrorMessage(error)}`)
  } finally {
    resolvingAlertId.value = null
  }
}

onMounted(() => {
  void refreshAll(false)
  clockTimer = setInterval(() => { clock.value = Date.now() }, 30_000)
})

// ── 诱捕层：幽灵访客与引流集合（只读读取 + 显式引流） ──
const decoy = ref<SecurityDecoyStatus | null>(null)
const decoyLoading = ref(false)
const decoyError = ref('')
const decoyApplying = ref(false)
const decoyReason = ref('诱捕命中累计，执行引流')
const decoyNotice = ref('')
let decoyGeneration = 0

async function loadDecoy(): Promise<void> {
  const generation = ++decoyGeneration
  decoyLoading.value = true
  decoyError.value = ''
  try {
    const data = await getDecoyStatus()
    if (generation !== decoyGeneration) return
    if (data.available !== true || data.verified !== true) {
      decoy.value = null
      const detail = (data.errors || []).join('；')
      decoyError.value = detail || '诱捕层状态尚未核验，请稍后重试'
      return
    }
    decoy.value = data
  } catch (error) {
    if (generation !== decoyGeneration) return
    decoy.value = null
    decoyError.value = getErrorMessage(error)
  } finally {
    if (generation === decoyGeneration) decoyLoading.value = false
  }
}

async function submitDecoyApply(): Promise<void> {
  const reason = decoyReason.value.trim()
  if (!reason) {
    decoyNotice.value = '请填写引流原因后再提交'
    return
  }
  decoyApplying.value = true
  decoyNotice.value = ''
  try {
    const result = await applyDecoyRedirect(reason)
    if (result.available !== true) {
      decoyNotice.value = (result.errors || []).join('；') || '引流未执行，请重试'
      return
    }
    const applied = result.applied.length
    const skipped = result.skipped.length
    decoyNotice.value = applied > 0
      ? `已引流 ${applied} 个来源${skipped ? `，跳过 ${skipped} 个（保护来源或非公网）` : ''}`
      : `没有可引流来源${skipped ? `，跳过 ${skipped} 个（保护来源或非公网）` : ''}`
  } catch (error) {
    decoyNotice.value = getErrorMessage(error)
  } finally {
    decoyApplying.value = false
    await loadDecoy()
  }
}

async function traceFromDecoy(ip: string): Promise<void> {
  activeSection.value = 'trace'
  traceIp.value = ip
  await submitTrace()
}

watch(activeSection, (section) => {
  if (section === 'decoy' && decoy.value === null && !decoyLoading.value) void loadDecoy()
})

onUnmounted(() => {
  if (clockTimer) clearInterval(clockTimer)
  ++eventRequestGeneration
  ++recentRequestGeneration
  ++overviewRequestGeneration
  ++serverRequestGeneration
  ++traceRequestGeneration
  ++surfaceRequestGeneration
  ++trafficRequestGeneration
})
</script>

<template>
  <main class="security-page" aria-labelledby="security-title">
    <header class="security-hero">
      <div class="hero-main">
        <div class="shield-art" aria-hidden="true">
          <svg viewBox="0 0 72 80" role="presentation"><path d="M36 4 63 14v21c0 18-11 31-27 40C20 66 9 53 9 35V14L36 4Z" /><path d="m24 39 8 8 17-19" /></svg>
          <span class="shield-spark">✦</span>
        </div>
        <div class="hero-copy">
          <span class="eyebrow">PRISM · SECURITY</span>
          <h2 id="security-title">小菱安全中心</h2>
          <p>掌握运行状态，查看安全事件，管理防御策略。</p>
          <div class="hero-badges">
            <span class="mode-badge"><Lock aria-hidden="true" />{{ securityModeLabel }}</span>
            <span class="mode-note">{{ blockingLabel }}</span>
          </div>
        </div>
      </div>
      <div class="hero-actions">
        <button class="button button-secondary" data-testid="refresh-all" type="button" :disabled="refreshing || runLoading || saveLoading" @click="refreshAll()">
          <Refresh :class="{ spinning: refreshing }" aria-hidden="true" /><span>{{ refreshing ? '刷新中…' : '刷新状态' }}</span>
        </button>
        <button class="button button-primary" data-testid="run-monitor" type="button" :disabled="runLoading || overviewLoading" @click="triggerMonitor">
          <Aim aria-hidden="true" /><span>{{ runLoading ? '巡检中…' : '立即巡检' }}</span>
        </button>
      </div>
    </header>

    <nav class="security-tabs" role="tablist" aria-label="安全中心视图">
      <button v-for="(section, index) in sections" :id="`security-tab-${section.id}`" :key="section.id" class="security-tab" type="button" role="tab"
        :aria-selected="activeSection === section.id" :aria-controls="`security-panel-${section.id}`" :tabindex="activeSection === section.id ? 0 : -1"
        @click="selectSection(section.id)" @keydown="handleTabKey($event, index)">{{ section.label }}</button>
    </nav>

    <section id="security-panel-overview" v-show="activeSection === 'overview'" class="section-content" role="tabpanel" aria-labelledby="security-tab-overview" tabindex="0">
      <div v-if="overviewError" class="state-banner error-banner" role="alert">
        <span>安全状态读取失败：{{ overviewError }}</span><button class="button button-secondary" data-testid="retry-overview" type="button" :disabled="overviewLoading" @click="loadOverview">重试</button>
      </div>
      <div class="metrics-grid" aria-label="安全状态概览" :aria-busy="overviewLoading">
        <article class="metric-card">
          <span class="metric-icon posture-icon"><DataAnalysis aria-hidden="true" /></span>
          <div class="metric-content"><span class="metric-label">监控状态</span>
            <strong class="metric-value metric-value-small" :class="`tone-${monitoringTone}`">{{ overviewLoading ? '读取中…' : monitoringLabel }}</strong>
            <small>{{ !overviewError && overview?.monitoring.enabled ? `${overview.monitoring.schedule_label}巡检` : '调度状态待核验' }}</small>
          </div>
        </article>
        <article class="metric-card">
          <span class="metric-icon alert-icon"><WarningFilled aria-hidden="true" /></span>
          <div class="metric-content"><span class="metric-label">24 小时待处理告警</span>
            <strong class="metric-value">{{ overviewLoading || overviewError || !overview ? '—' : overview.open_alerts_24h }}</strong>
            <small>全部待处理 {{ overviewError ? '—' : overview?.open_alerts_total ?? '—' }} 条</small>
          </div>
        </article>
        <article class="metric-card">
          <span class="metric-icon source-icon"><CircleCheck aria-hidden="true" /></span>
          <div class="metric-content"><span class="metric-label">数据源状态</span>
            <strong class="metric-value metric-value-small">{{ overviewLoading ? '读取中…' : coverageLabel }}</strong>
            <small>{{ overviewError ? '—' : overview?.monitoring.sources.length ?? '—' }} 个采集来源</small>
          </div>
        </article>
        <article class="metric-card">
          <span class="metric-icon time-icon"><Clock aria-hidden="true" /></span>
          <div class="metric-content"><span class="metric-label">最近巡检</span>
            <strong class="metric-value metric-value-small">{{ overviewLoading ? '读取中…' : overview ? lastRunLabel.split(' · ')[0] : '状态未知' }}</strong>
            <small>{{ !overviewError && overview?.monitoring.last_run.finished_at ? formatTime(overview.monitoring.last_run.finished_at) : '尚无有效完成时间' }}</small>
          </div>
        </article>
      </div>

      <div class="overview-columns">
        <section class="runtime-panel panel" aria-labelledby="runtime-title" :aria-busy="serverLoading">
          <div class="section-heading"><div><h3 id="runtime-title">运行环境资源</h3><p class="section-subtitle">当前运行环境的资源快照</p></div></div>
          <div v-if="serverError" class="state-banner error-banner" role="alert"><span>运行环境资源读取失败：{{ serverError }}</span><button class="button button-secondary" data-testid="retry-server" type="button" :disabled="serverLoading" @click="loadServer">重试</button></div>
          <div class="runtime-grid">
            <article v-for="metric in resourceMetrics" :key="metric.key" class="runtime-metric" :data-testid="`resource-${metric.id}`">
              <div class="runtime-topline"><span>{{ metric.label }}</span><strong>{{ resourceLabel(metric.key) }}</strong></div>
              <progress v-if="resourceValue(metric.key) !== null" :value="Math.min(resourceValue(metric.key) ?? 0, 100)" max="100" :aria-label="metric.label">{{ resourceLabel(metric.key) }}</progress>
              <div v-else class="progress-placeholder" aria-hidden="true"></div>
              <small v-if="metric.id === 'disk' && !serverError && systemStatus?.available">{{ systemStatus.disk_used_gb ?? '—' }} / {{ systemStatus.disk_total_gb ?? '—' }} GB</small>
            </article>
            <article class="runtime-metric uptime-metric" data-testid="resource-uptime"><span>服务器运行时长</span><strong>{{ uptimeLabel }}</strong><small>当前采集进程视角</small></article>
          </div>
          <p class="panel-footnote">{{ !serverError && systemStatus?.collected_at ? `采集于 ${formatTime(systemStatus.collected_at)}` : '等待有效采集结果' }}</p>
        </section>
        <section class="sources-panel panel" aria-labelledby="sources-title" :aria-busy="overviewLoading">
          <div class="section-heading"><div><h3 id="sources-title">数据源状态</h3><p class="section-subtitle">{{ overviewLoading ? '正在读取采集结果…' : coverageLabel }}</p></div></div>
          <div v-if="overviewLoading && !overview" class="empty-state" role="status">正在读取数据源…</div>
          <ul v-else-if="overview?.monitoring.sources.length" class="source-list">
            <li v-for="source in overview.monitoring.sources" :key="source.code"><span class="source-dot" :class="`tone-${sourceStatusTone(source.status)}`" aria-hidden="true"></span><span class="source-name">{{ source.label }}</span><span class="state-pill" :class="`tone-${sourceStatusTone(source.status)}`">{{ sourceStatusLabel(source.status) }}</span></li>
          </ul>
          <div v-else class="empty-state">尚无数据源记录</div>
          <button class="button button-text source-inspection" type="button" @click="viewInspectionEvents">查看巡检与采集记录</button>
        </section>
      </div>

      <section class="recent-panel panel" aria-labelledby="recent-title" :aria-busy="recentLoading">
        <div class="section-heading"><div><h3 id="recent-title">最近活动</h3><p class="section-subtitle">近 24 小时的最新 5 条安全活动</p></div><button class="button button-text" data-testid="view-all-events" type="button" @click="viewAllEvents">查看全部记录 <span aria-hidden="true">→</span></button></div>
        <div v-if="recentError" class="state-banner error-banner" role="alert"><span>最近活动读取失败：{{ recentError }}</span><button class="button button-secondary" type="button" :disabled="recentLoading" @click="loadRecentEvents">重试</button></div>
        <div v-else-if="recentLoading" class="empty-state" role="status">正在读取最近活动…</div>
        <div v-else-if="!recentEvents.length" class="empty-state"><Lock aria-hidden="true" /><strong>近 24 小时暂无活动记录</strong><p>可在事件记录中选择更长时间范围。</p></div>
        <ol v-else class="recent-list">
          <li v-for="event in recentEvents" :key="event.id"><span class="event-rail" :class="`sev-${event.severity}`" aria-hidden="true"></span><div class="recent-main"><h4>{{ event.title }}</h4><span>{{ event.layer }} · {{ event.actor }}</span></div><time :datetime="event.recorded_at || undefined">{{ formatTime(event.recorded_at) }}</time><span class="state-pill" :class="`status-${event.status}`">{{ eventStatusLabel(event.status) }}</span></li>
        </ol>
      </section>
    </section>

    <section id="security-panel-events" v-show="activeSection === 'events'" class="section-content" role="tabpanel" aria-labelledby="security-tab-events" tabindex="0">
      <section class="timeline-panel panel" aria-labelledby="events-title">
        <div class="section-heading"><div><h3 id="events-title">事件记录</h3><p class="section-subtitle">按记录时间排序，展开查看详情与处理记录。</p></div></div>
        <div class="event-toolbar">
          <label class="filter-field"><span>记录类型</span><select v-model="eventGroup" data-testid="event-group" @change="refreshEvents"><option value="activity">安全活动</option><option value="inspection">巡检与采集</option><option value="all">全部记录</option></select></label>
          <label class="filter-field"><span>时间范围</span><select v-model.number="hours" data-testid="event-range" @change="refreshEvents"><option :value="24">近 24 小时</option><option :value="168">近 7 天</option><option :value="720">近 30 天</option></select></label>
          <span class="event-count">{{ eventsLoading ? '正在读取…' : eventPage ? `${eventPage.truncated ? '至少' : '共'} ${eventPage.total} 条记录` : '等待查询结果' }}</span>
        </div>
        <div v-if="eventsError" class="state-banner error-banner" role="alert"><span>事件记录读取失败：{{ eventsError }}</span><button class="button button-secondary" data-testid="retry-events" type="button" :disabled="eventsLoading" @click="loadEvents">重试</button></div>
        <div v-else-if="eventsLoading" class="empty-state" role="status">正在读取事件记录…</div>
        <div v-else-if="!eventPage?.items.length" class="empty-state"><Lock aria-hidden="true" /><strong>当前筛选下暂无记录</strong><p>调整记录类型或时间范围，查看其他记录。</p></div>
        <ol v-else class="event-list" :aria-busy="eventsLoading">
          <li v-for="event in eventPage.items" :key="event.id" class="event-item">
            <span class="event-marker" :class="`sev-${event.severity}`" aria-hidden="true"><WarningFilled v-if="['critical', 'high', 'warning'].includes(event.severity)" /><CircleCheck v-else /></span>
            <div class="event-main">
              <div class="event-topline"><div class="event-title-group"><span class="event-layer">{{ event.layer }}</span><h4>{{ event.title }}</h4></div><time :datetime="event.recorded_at || undefined">{{ formatTime(event.recorded_at) }}</time></div>
              <p class="event-summary">{{ event.summary }}</p>
              <details class="event-details"><summary>查看详情</summary><div class="event-detail-content">
                <p class="event-actor">记录方：{{ event.actor }}<span v-if="event.action_code"> · 审计动作：<code>{{ event.action_code }}</code></span></p>
                <dl v-if="evidenceEntries(event).length" class="evidence-grid"><div v-for="[label, value] in evidenceEntries(event)" :key="label"><dt>{{ label }}</dt><dd>{{ value }}</dd></div></dl>
                <p v-else class="panel-footnote">本条记录未附补充详情。</p>
                <p v-if="event.resolution?.note" class="resolution-note">处理备注：{{ event.resolution.note }}<span>{{ event.resolution.resolved_by_name || '管理员' }} · {{ formatTime(event.resolution.resolved_at) }}</span></p>
              </div></details>
            </div>
            <span class="event-status state-pill" :class="`status-${event.status}`">{{ eventStatusLabel(event.status) }}</span>
            <div class="event-actions"><button v-if="event.event_type === 'alert' && event.status === 'open' && event.alert_id" class="button button-secondary resolve-button" type="button" :disabled="resolvingAlertId !== null" @click="resolveSecurityAlert(event)">{{ resolvingAlertId === event.alert_id ? '保存中…' : '标记已处理' }}</button><span v-else class="action-placeholder">{{ event.status === 'resolved' ? '处理记录见详情' : '无需处理' }}</span></div>
          </li>
        </ol>
        <div v-if="eventPage && eventPage.total > 0" class="timeline-footer"><span>{{ eventPage.truncated ? '记录达到读取上限，可缩小时间范围。' : `共 ${eventPage.total} 条记录，每页 ${eventPage.page_size} 条` }}</span><div class="pager" aria-label="事件分页"><button class="button button-secondary" type="button" :disabled="page <= 1 || eventsLoading" @click="changePage(page - 1)">上一页</button><span aria-live="polite">{{ page }} / {{ Math.max(eventPage.pages, 1) }}</span><button class="button button-secondary" data-testid="next-page" type="button" :disabled="page >= eventPage.pages || eventsLoading" @click="changePage(page + 1)">下一页</button></div></div>
      </section>
    </section>

    <section id="security-panel-strategies" v-show="activeSection === 'strategies'" class="section-content" role="tabpanel" aria-labelledby="security-tab-strategies" tabindex="0">
      <AutomaticBlockingPanel ref="blockingPanel" @status="Object.assign(blockingStatus, $event)" @changed="refreshRecordedEvents()" />
      <div class="policy-columns">
        <section class="policy-panel panel" aria-labelledby="policy-title">
          <div class="section-heading"><div><h3 id="policy-title">监控灵敏度</h3><p class="section-subtitle">调整 SSH 与 Nginx 告警的检测阈值。</p></div><span class="permission-note"><Lock aria-hidden="true" />仅超级管理员</span></div>
          <div v-if="overviewError" class="state-banner error-banner" role="alert"><span>策略状态读取失败：{{ overviewError }}</span><button class="button button-secondary" type="button" :disabled="overviewLoading" @click="loadOverview">重试</button></div>
          <div v-if="overviewLoading && !overview" class="empty-state" role="status">正在读取当前策略…</div>
          <form v-else class="policy-form" @submit.prevent="savePolicy">
            <label><span>SSH 登录失败阈值</span><div class="input-suffix"><input v-model.number="policyDraft.ssh_failed_threshold" name="ssh_failed_threshold" type="number" min="1" :max="overview?.policy.ssh_failed_threshold || 5000" :disabled="!overview || !!overviewError || saveLoading || overviewLoading" required><em>次</em></div><small>累计失败达到阈值时告警</small></label>
            <label><span>SSH 观察窗口</span><div class="input-suffix"><input v-model.number="policyDraft.ssh_window_hours" name="ssh_window_hours" type="number" :min="overview?.policy.ssh_window_hours || 1" max="24" :disabled="!overview || !!overviewError || saveLoading || overviewLoading" required><em>小时</em></div><small>统计登录失败的时间范围</small></label>
            <label><span>Nginx 异常请求阈值</span><div class="input-suffix"><input v-model.number="policyDraft.nginx_failure_threshold" name="nginx_failure_threshold" type="number" min="1" :max="overview?.policy.nginx_failure_threshold || 5000" :disabled="!overview || !!overviewError || saveLoading || overviewLoading" required><em>次</em></div><small>异常请求达到阈值时告警</small></label>
            <label><span>Nginx 观察窗口</span><div class="input-suffix"><input v-model.number="policyDraft.nginx_window_hours" name="nginx_window_hours" type="number" :min="overview?.policy.nginx_window_hours || 1" max="24" :disabled="!overview || !!overviewError || saveLoading || overviewLoading" required><em>小时</em></div><small>统计异常请求的时间范围</small></label>
            <div class="policy-form-footer full-field"><p class="policy-draft-state" role="status">{{ overviewLoading ? '正在读取当前配置…' : overviewError || !overview ? '策略状态待核验' : policyDirty ? '有未保存修改；刷新状态会保留草稿。' : '当前配置已同步' }}</p><div class="policy-actions"><button class="button button-secondary" data-testid="reset-policy" type="button" :disabled="!policyDirty || saveLoading || !overview || !!overviewError" @click="resetPolicy">恢复当前配置</button><button class="button button-primary" data-testid="save-policy" type="submit" :disabled="saveLoading || overviewLoading || !overview || !!overviewError || !policyDirty">{{ saveLoading ? '保存中…' : '保存变更' }}</button></div></div>
            <p v-if="policyFeedback" class="policy-save-feedback full-field" :class="{ 'feedback-error': policyFeedbackError }" :role="policyFeedbackError ? 'alert' : 'status'">{{ policyFeedback }}</p>
          </form>
        </section>
        <aside class="policy-info panel" aria-labelledby="policy-info-title">
          <div class="section-heading"><div><h3 id="policy-info-title">策略状态</h3><p class="section-subtitle">当前约束与配置来源</p></div></div>
          <dl class="policy-facts"><div><dt>数据采集</dt><dd>只读</dd></div><div><dt>通知门槛</dt><dd>{{ overviewError || !overview ? '状态未知' : popupSeverityLabel }}</dd></div><div><dt>自动封禁</dt><dd>{{ blockingLabel.replace('自动封禁：', '') }}</dd></div><div><dt>反击操作</dt><dd>未启用</dd></div><div><dt>配置来源</dt><dd>{{ overviewError || !overview ? '状态未知' : overview.policy.source === 'environment' ? '部署默认值' : overview.policy.source === 'database' ? '管理员配置' : '安全回退值' }}</dd></div><div><dt>策略版本</dt><dd>{{ overviewError ? '—' : overview?.policy.revision ?? '—' }}</dd></div></dl>
          <p class="policy-updated">{{ !overviewError && overview?.policy.updated_at ? `最近由 ${overview.policy.updated_by || '管理员'} 更新 · ${formatTime(overview.policy.updated_at)}` : '尚无人工变更记录' }}</p>
          <details class="policy-explanation"><summary>查看配置规则</summary><p>降低失败次数阈值或扩大观察窗口会提高检测灵敏度。阈值不能高于当前配置，窗口不能短于当前配置，最长为 24 小时。</p><p>监控灵敏度用于生成告警；临时自动封禁由独立策略管理，变更会保存操作审计。</p></details>
        </aside>
      </div>
    </section>

    <section id="security-panel-trace" v-show="activeSection === 'trace'" class="section-content" role="tabpanel" aria-labelledby="security-tab-trace" tabindex="0">
      <section class="trace-query-panel panel" aria-labelledby="trace-query-title">
        <div class="section-heading">
          <div><h3 id="trace-query-title">来源溯源</h3><p class="section-subtitle">输入单个来源 IP，被动读取本机可信日志、处置回执与外部情报。</p></div>
          <span class="permission-note"><Lock aria-hidden="true" />仅超级管理员</span>
        </div>
        <form class="trace-form" @submit.prevent="submitTrace">
          <label class="filter-field trace-ip-field" for="trace-ip-input"><span>来源 IP</span>
            <input id="trace-ip-input" v-model="traceIp" data-testid="trace-ip-input" name="trace_ip" type="text" autocomplete="off" spellcheck="false" placeholder="例如 203.0.113.9" :disabled="traceLoading" :aria-invalid="traceInlineError ? 'true' : undefined" aria-describedby="trace-ip-help" @input="traceInlineError = ''">
          </label>
          <button class="button button-primary" data-testid="trace-ip-submit" type="submit" :disabled="traceLoading"><Search aria-hidden="true" /><span>{{ traceLoading ? '溯源中…' : '开始溯源' }}</span></button>
        </form>
        <p id="trace-ip-help" class="trace-help">只接受单个 IPv4 或 IPv6 地址，不接受网段、路径或命令。</p>
        <p v-if="traceInlineError" class="trace-inline-error" data-testid="trace-ip-error" role="alert">{{ traceInlineError }}</p>
        <p class="trace-readonly" data-testid="trace-readonly"><Lock aria-hidden="true" /><span>本溯源全程只读，不向目标发送任何扫描或探测请求。</span></p>

        <div v-if="traceError" class="state-banner error-banner" role="alert">
          <span>{{ traceError }}</span>
          <button class="button button-secondary" data-testid="retry-trace" type="button" :disabled="traceLoading" @click="retryTrace">重试</button>
        </div>
        <div v-else-if="traceLoading" class="empty-state" role="status">正在读取本机可信日志与处置回执…</div>
        <article v-else-if="traceResult" class="trace-result" data-testid="trace-result">
          <header class="trace-result-head">
            <div class="trace-result-identity">
              <code class="trace-ip">{{ traceResult.ip }}</code>
              <span class="state-pill" :class="traceResult.is_public ? 'tone-warning' : 'tone-unknown'">{{ traceResult.is_public ? '公网来源' : '非公网地址' }}</span>
            </div>
            <div class="trace-risk">
              <span class="state-pill" :class="`risk-${riskLevelTone(traceResult.risk.level)}`">{{ riskLevelLabel(traceResult.risk.level) }}</span>
              <strong class="trace-risk-score">{{ traceResult.risk.score }}</strong><small>风险分</small>
              <progress :value="Math.min(Math.max(traceResult.risk.score, 0), 100)" max="100" aria-label="风险分">{{ traceResult.risk.score }}</progress>
            </div>
          </header>
          <p class="trace-meta">生成时间 {{ formatTime(traceResult.generated_at) }} · 观察窗口 {{ traceResult.window_hours }} 小时{{ traceResult.request_id ? ` · 执行记录 ${traceResult.request_id}` : '' }}</p>
          <p v-if="traceResult.is_protected" class="trace-protected" data-testid="trace-protected" role="status"><Lock aria-hidden="true" /><span>受保护来源，不参与自动处置</span></p>

          <div class="trace-columns">
            <section class="trace-block" aria-labelledby="trace-risk-title">
              <h4 id="trace-risk-title">风险理由</h4>
              <ul v-if="traceResult.risk.reasons.length" class="trace-reason-list"><li v-for="reason in traceResult.risk.reasons" :key="reason">{{ reason }}</li></ul>
              <p v-else class="trace-muted">本机日志未记录可解释的风险理由。</p>
              <p class="trace-basis" data-testid="trace-risk-basis"><strong>评分依据</strong>{{ traceResult.risk.basis || '未提供评分依据' }}。评分只使用本机可信日志，外部被动归因不参与评分。</p>
            </section>
            <section class="trace-block" aria-labelledby="trace-attribution-title">
              <h4 id="trace-attribution-title">归属与情报</h4>
              <p class="trace-attribution-note" data-testid="trace-attribution-note">外部被动归因，不参与评分</p>
              <dl v-if="traceResult.attribution.ok" class="trace-facts">
                <div v-if="traceResult.attribution.country"><dt>国家/地区</dt><dd>{{ traceResult.attribution.country }}</dd></div>
                <div v-if="traceResult.attribution.region"><dt>区域</dt><dd>{{ traceResult.attribution.region }}</dd></div>
                <div v-if="traceResult.attribution.city"><dt>城市</dt><dd>{{ traceResult.attribution.city }}</dd></div>
                <div v-if="traceResult.attribution.isp"><dt>ISP</dt><dd>{{ traceResult.attribution.isp }}</dd></div>
                <div v-if="traceResult.attribution.org"><dt>Org</dt><dd>{{ traceResult.attribution.org }}</dd></div>
                <div v-if="traceResult.attribution.as"><dt>AS</dt><dd>{{ traceResult.attribution.as }}</dd></div>
              </dl>
              <p v-else class="trace-muted">{{ traceResult.attribution.note || '未取得外部归因结果。' }}</p>
              <dl class="trace-facts trace-intel">
                <div><dt>反向解析</dt><dd>{{ traceResult.reverse_dns.ok ? (traceResult.reverse_dns.output || '已解析，无输出') : (traceResult.reverse_dns.note || '未取得反向解析') }}</dd></div>
                <div><dt>Whois</dt><dd>{{ traceResult.whois.ok ? (traceResult.whois.summary || '已查询，无摘要') : (traceResult.whois.note || '未取得 Whois 结果') }}</dd></div>
              </dl>
            </section>
          </div>

          <div class="trace-columns">
            <section class="trace-block" data-testid="trace-ssh" aria-labelledby="trace-ssh-title">
              <h4 id="trace-ssh-title">SSH 证据</h4>
              <p class="trace-metric-line"><strong>{{ traceResult.ssh.count }}</strong><span>次认证失败</span></p>
              <p class="trace-muted">首次 {{ formatTime(traceResult.ssh.first_seen) }} · 最近 {{ formatTime(traceResult.ssh.last_seen) }}</p>
              <ul v-if="traceResult.ssh.accounts_tried.length" class="trace-tag-list"><li v-for="item in traceResult.ssh.accounts_tried" :key="item.account"><code>{{ item.account }}</code><span>{{ item.count }} 次</span></li></ul>
              <p v-else class="trace-muted">未记录被尝试的账号。</p>
            </section>
            <section class="trace-block" data-testid="trace-web" aria-labelledby="trace-web-title">
              <h4 id="trace-web-title">Web 证据</h4>
              <p class="trace-metric-line"><strong>{{ traceResult.web.count }}</strong><span>次异常请求 · {{ traceResult.web.target_count }} 个不同目标</span></p>
              <p class="trace-muted">首次 {{ formatTime(traceResult.web.first_seen) }} · 最近 {{ formatTime(traceResult.web.last_seen) }}</p>
              <ul v-if="traceResult.web.targets.length" class="trace-tag-list"><li v-for="item in traceResult.web.targets" :key="item.path"><code>{{ item.path }}</code><span>{{ item.count }} 次</span></li></ul>
              <p v-else class="trace-muted">未记录探测目标路径。</p>
              <dl class="trace-facts trace-intel">
                <div><dt>方法分布</dt><dd>{{ countEntries(traceResult.web.methods, 8).map(([method, count]) => `${method} × ${count}`).join('、') || '未记录' }}</dd></div>
                <div><dt>状态码分布</dt><dd>{{ countEntries(traceResult.web.status_codes, 8).map(([code, count]) => `${code} × ${count}`).join('、') || '未记录' }}</dd></div>
              </dl>
            </section>
          </div>

          <section class="trace-block trace-records" aria-labelledby="trace-records-title">
            <h4 id="trace-records-title">处置记录</h4>
            <p v-if="!traceResult.defense_records.length" class="trace-muted">本机处置回执中没有该来源的封禁或解封记录。</p>
            <div v-else class="trace-table-wrap">
              <table class="trace-table">
                <caption class="visually-hidden">该来源的防火墙与封禁处置记录</caption>
                <thead><tr><th scope="col">规则</th><th scope="col">来源</th><th scope="col">状态</th><th scope="col">开始时间</th><th scope="col">到期时间</th><th scope="col">解封时间</th><th scope="col">证据数</th><th scope="col">原因</th></tr></thead>
                <tbody><tr v-for="record in traceResult.defense_records" :key="record.id">
                  <td>{{ record.rule }}</td><td>{{ record.source }}</td>
                  <td><span class="state-pill" :class="`status-${record.status}`">{{ eventStatusLabel(record.status) }}</span></td>
                  <td>{{ formatTime(record.started_at) }}</td><td>{{ formatTime(record.expires_at) }}</td><td>{{ formatTime(record.released_at) }}</td>
                  <td>{{ record.evidence_count }}</td><td class="trace-reason-cell">{{ record.reason || '未记录原因' }}</td>
                </tr></tbody>
              </table>
            </div>
          </section>

          <details v-if="traceResult.evidence_sources.length || traceResult.errors.length" class="trace-diagnostics">
            <summary>证据来源与诊断</summary>
            <p v-if="traceResult.evidence_sources.length" class="trace-muted">证据来源：{{ traceResult.evidence_sources.join('、') }}</p>
            <ul v-if="traceResult.errors.length" class="trace-diagnostic-list"><li v-for="(item, index) in traceResult.errors" :key="index">{{ item }}</li></ul>
          </details>
        </article>
        <p v-else class="empty-state"><Search aria-hidden="true" /><strong>尚未提交溯源请求</strong><span>溯源结果完全来自本机日志与回执，不含任何主动探测数据。</span></p>
      </section>

      <section class="trace-surface-panel panel" data-testid="surface-panel" aria-labelledby="surface-title" :aria-busy="surfaceLoading">
        <div class="section-heading">
          <div><h3 id="surface-title">本机防御面</h3><p class="section-subtitle">只读读取监听端口、防火墙链、加固应用与拦截现状。</p></div>
          <button class="button button-secondary" data-testid="refresh-surface" type="button" :disabled="surfaceLoading" @click="loadSurface"><Refresh :class="{ spinning: surfaceLoading }" aria-hidden="true" /><span>{{ surfaceLoading ? '读取中…' : '刷新防御面' }}</span></button>
        </div>
        <div v-if="surfaceError" class="state-banner error-banner" role="alert">
          <span>{{ surfaceError }}</span>
          <button class="button button-secondary" data-testid="retry-surface" type="button" :disabled="surfaceLoading" @click="loadSurface">重试</button>
        </div>
        <div v-else-if="surfaceLoading && !surface" class="empty-state" role="status">正在读取本机防御面…</div>
        <template v-else-if="surface">
          <dl class="surface-facts">
            <div><dt>对外监听端口</dt><dd data-testid="surface-public-listeners">{{ surface.public_listener_count }} 个</dd></div>
            <div><dt>自动封禁</dt><dd>{{ surface.blocking.enabled ? '已启用' : '未启用' }} · 后端 {{ surface.blocking.backend || '未识别' }} · 实际租约 {{ surface.blocking.active_leases }} 条</dd></div>
            <div><dt>SSH 端口</dt><dd>{{ surface.ssh_ports || '未读取到' }}</dd></div>
            <div><dt>ipset</dt><dd>{{ surface.ipset.present ? `已就绪 · ${surface.ipset.sets.length ? surface.ipset.sets.join('、') : '暂无集合'}` : '未安装' }}</dd></div>
          </dl>

          <section class="surface-section" aria-labelledby="surface-listeners-title">
            <div class="surface-subheading">
              <h4 id="surface-listeners-title">监听端口</h4>
              <label class="surface-filter"><input v-model="publicListenersOnly" data-testid="surface-public-only" type="checkbox"><span>只看对外监听</span></label>
            </div>
            <p v-if="!visibleListeners.length" class="trace-muted">{{ surface.listeners.length ? '当前筛选下没有对外监听端口。' : '回执未返回监听端口，请核验采集权限。' }}</p>
            <div v-else class="trace-table-wrap">
              <table class="trace-table">
                <thead><tr><th scope="col">协议</th><th scope="col">地址</th><th scope="col">端口</th><th scope="col">进程</th><th scope="col">暴露面</th></tr></thead>
                <tbody><tr v-for="listener in visibleListeners" :key="`${listener.protocol}-${listener.address}-${listener.port}`" :class="{ 'is-public': isPublicListener(listener) }">
                  <td>{{ listener.protocol || '未知' }}</td><td>{{ listener.address }}</td><td>{{ listener.port }}</td><td>{{ listener.process || '未知' }}</td>
                  <td><span class="state-pill" :class="isPublicListener(listener) ? 'status-open' : 'tone-healthy'">{{ isPublicListener(listener) ? '对外监听' : '仅本机/内网' }}</span></td>
                </tr></tbody>
              </table>
            </div>
          </section>

          <section class="surface-section" aria-labelledby="surface-apps-title">
            <h4 id="surface-apps-title">加固应用</h4>
            <p v-if="!surface.applications.length" class="trace-muted">回执未返回加固应用清单。</p>
            <ul v-else class="surface-app-list">
              <li v-for="application in surface.applications" :key="application.name" :class="{ installed: application.installed }">
                <span class="app-name">{{ application.name }}</span><span class="app-purpose">{{ application.purpose }}</span>
                <span class="state-pill" :class="application.installed ? 'tone-healthy' : 'tone-unknown'">{{ application.installed ? '已安装' : '未安装' }}</span>
              </li>
            </ul>
          </section>

          <section class="surface-section" aria-labelledby="surface-firewall-title">
            <h4 id="surface-firewall-title">防火墙链</h4>
            <div v-for="family in firewallFamilies" :key="family.key" class="firewall-family">
              <p class="firewall-head"><strong>{{ family.label }}</strong><span>{{ surface.firewall[family.key].tool || '工具未识别' }}</span><span>{{ surface.firewall[family.key].tools_present.length ? `已检测 ${surface.firewall[family.key].tools_present.join('、')}` : '未检测到工具' }}</span></p>
              <p v-if="!surface.firewall[family.key].chains.length" class="trace-muted">未读取到链信息。</p>
              <ul v-else class="firewall-chains">
                <li v-for="chain in surface.firewall[family.key].chains" :key="`${family.key}-${chain.chain}`">
                  <span class="state-pill" :class="chain.ok ? 'tone-healthy' : 'status-failed'">{{ chain.ok ? '可读' : '不可读' }}</span>
                  <code>{{ chain.chain }}</code><span>策略 {{ chain.policy || '未知' }}</span><span>{{ chain.rules.length }} 条规则</span>
                  <span v-if="chain.note" class="chain-note">{{ chain.note }}</span>
                </li>
              </ul>
            </div>
          </section>

          <details v-if="surface.errors.length" class="trace-diagnostics">
            <summary>防御面诊断（{{ surface.errors.length }} 项）</summary>
            <ul class="trace-diagnostic-list"><li v-for="(item, index) in surface.errors" :key="index">{{ item }}</li></ul>
          </details>
          <p class="panel-footnote">防御面读取于 {{ formatTime(surface.generated_at) }}，仅展示回执原文，不推断未返回的数据。</p>
        </template>
        <p v-else class="empty-state">尚未读取防御面。</p>
      </section>

      <section class="trace-traffic-panel panel" data-testid="traffic-panel" aria-labelledby="traffic-title" :aria-busy="trafficLoading">
        <div class="section-heading">
          <div><h3 id="traffic-title">流量元数据</h3><p class="section-subtitle">对端连接、端口协议与日志计数的汇总视图。</p></div>
          <div class="traffic-controls">
            <label class="filter-field" for="traffic-range"><span>时间范围</span>
              <select id="traffic-range" v-model.number="trafficHours" data-testid="traffic-range" :disabled="trafficLoading" @change="loadTraffic"><option :value="24">近 24 小时</option><option :value="48">近 48 小时</option><option :value="72">近 72 小时</option></select>
            </label>
            <button class="button button-secondary" data-testid="refresh-traffic" type="button" :disabled="trafficLoading" @click="loadTraffic"><Refresh :class="{ spinning: trafficLoading }" aria-hidden="true" /><span>{{ trafficLoading ? '读取中…' : '刷新流量' }}</span></button>
          </div>
        </div>
        <p class="traffic-payload-note" :class="{ 'payload-alert': Boolean(traffic && traffic.payload_captured !== false) }" data-testid="traffic-payload-note">
          <Lock aria-hidden="true" />
          <span v-if="traffic && traffic.payload_captured === false">只采集连接元数据与日志计数，不捕获、不存储流量载荷。</span>
          <span v-else-if="traffic">回执显示存在流量载荷采集，请立即核验采集配置。</span>
          <span v-else>本面板只读取连接元数据与日志计数，载荷采集状态待核验。</span>
        </p>
        <div v-if="trafficError" class="state-banner error-banner" role="alert">
          <span>{{ trafficError }}</span>
          <button class="button button-secondary" data-testid="retry-traffic" type="button" :disabled="trafficLoading" @click="loadTraffic">重试</button>
        </div>
        <div v-else-if="trafficLoading && !traffic" class="empty-state" role="status">正在读取连接元数据…</div>
        <template v-else-if="traffic">
          <dl class="traffic-facts">
            <div><dt>对端总数</dt><dd data-testid="traffic-peer-total">{{ traffic.peer_total }}</dd></div>
            <div><dt>当前连接数</dt><dd>{{ traffic.current_connections }}</dd></div>
            <div><dt>近期 SSH 失败来源</dt><dd>{{ traffic.recent_ssh_failed_sources }}</dd></div>
            <div><dt>近期敏感探测来源</dt><dd>{{ traffic.recent_probe_sources }}</dd></div>
            <div><dt>观察窗口</dt><dd>{{ traffic.window_hours }} 小时</dd></div>
          </dl>
          <p v-if="traffic.note" class="panel-footnote">{{ traffic.note }}</p>
          <p v-if="!traffic.peers.length" class="empty-state">当前窗口内没有对端连接记录。</p>
          <div v-else class="trace-table-wrap">
            <table class="trace-table">
              <thead><tr><th scope="col">对端 IP</th><th scope="col">当前连接数</th><th scope="col">SSH 失败</th><th scope="col">敏感探测</th><th scope="col">不同目标数</th><th scope="col">端口 / 协议</th><th scope="col">最近时间</th></tr></thead>
              <tbody><tr v-for="peer in traffic.peers" :key="peer.ip">
                <td><code>{{ peer.ip }}</code></td>
                <td>{{ peer.connections }}</td><td>{{ peer.ssh_failed_count }}</td><td>{{ peer.sensitive_probe_count }}</td><td>{{ peer.target_count }}</td>
                <td class="trace-endpoint-cell">{{ peerEndpointSummary(peer) }}<small v-if="peerProcessSummary(peer)">{{ peerProcessSummary(peer) }}</small></td>
                <td>{{ formatTime(peer.last_seen) }}</td>
              </tr></tbody>
            </table>
          </div>
          <details v-if="traffic.errors.length" class="trace-diagnostics">
            <summary>采集诊断（{{ traffic.errors.length }} 项）</summary>
            <ul class="trace-diagnostic-list"><li v-for="(item, index) in traffic.errors" :key="index">{{ item }}</li></ul>
          </details>
          <p class="panel-footnote">数据读取于 {{ formatTime(traffic.generated_at) }}，仅统计元数据与日志计数。</p>
        </template>
        <p v-else class="empty-state">尚未读取流量元数据。</p>
      </section>
    </section>

    <section id="security-panel-decoy" v-show="activeSection === 'decoy'" class="section-content" role="tabpanel" aria-labelledby="security-tab-decoy" tabindex="0">
      <section class="timeline-panel panel" aria-labelledby="decoy-title" :aria-busy="decoyLoading">
        <div class="section-heading">
          <div>
            <h3 id="decoy-title">诱捕层</h3>
            <p class="section-subtitle">命中诱饵的来源留在虚构环境里；真业务对这些来源不生效。</p>
          </div>
          <button class="button button-secondary" data-testid="refresh-decoy" type="button" :disabled="decoyLoading" @click="loadDecoy">
            <Refresh :class="{ spinning: decoyLoading }" aria-hidden="true" /><span>刷新诱捕层</span>
          </button>
        </div>
        <div v-if="decoyError" class="state-banner error-banner" role="alert">
          <span>诱捕层状态读取失败：{{ decoyError }}</span>
          <button class="button button-secondary" data-testid="retry-decoy" type="button" :disabled="decoyLoading" @click="loadDecoy">重试</button>
        </div>
        <template v-else-if="decoy">
          <dl class="runtime-metrics decoy-metrics">
            <div class="runtime-metric" data-testid="decoy-container">
              <span>诱捕容器</span><strong>{{ decoy.container_running ? '运行中' : '未运行' }}</strong>
              <small>{{ decoy.container_running ? '受内核改写的流量会落到这里' : '容器未启动时引流不生效' }}</small>
            </div>
            <div class="runtime-metric" data-testid="decoy-hits">
              <span>诱饵命中总次数</span><strong>{{ decoy.hit_total }}</strong><small>来源 {{ decoy.source_total }} 个</small>
            </div>
            <div class="runtime-metric" data-testid="decoy-redirect">
              <span>已引流来源</span><strong>{{ decoy.redirect_total }}</strong>
              <small>集合 {{ decoy.redirect_chain }} · 端口 {{ decoy.redirect_port }}</small>
            </div>
            <div class="runtime-metric" data-testid="decoy-chain">
              <span>引流链序</span><strong>{{ decoy.chain_order_ok ? '正确' : '异常' }}</strong>
              <small>{{ decoy.chain_order_ok ? '排在 DROP 链之前' : '必须排在 DROP 链之前，否则会被丢弃' }}</small>
            </div>
          </dl>
          <p class="panel-footnote">日志：{{ decoy.log_path }} · 读取于 {{ formatTime(decoy.generated_at) }}</p>
          <div class="decoy-apply">
            <label class="filter-field" for="decoy-reason">
              <span>引流原因</span>
              <input id="decoy-reason" v-model="decoyReason" data-testid="decoy-reason" type="text" maxlength="200" :disabled="decoyApplying" />
            </label>
            <button class="button button-primary" data-testid="decoy-apply" type="button" :disabled="decoyApplying || decoyLoading" @click="submitDecoyApply">
              {{ decoyApplying ? '执行中…' : '把命中来源加入引流' }}
            </button>
            <p v-if="decoyNotice" class="policy-draft-state" role="status" data-testid="decoy-notice">{{ decoyNotice }}</p>
          </div>
          <p class="panel-footnote">
            引流只针对命中诱饵的公网来源；当前管理员连接、服务器本机地址、白名单与已建立连接的公网对端一律跳过。
            租约由内核 TTL 自动到期，无需人工解封。
          </p>
          <h4 class="decoy-subtitle">幽灵访客</h4>
          <p v-if="!decoy.sources.length" class="empty-state" data-testid="decoy-empty">近 24 小时没有诱饵命中。</p>
          <div v-else class="table-scroll">
            <table class="decoy-table" data-testid="decoy-sources">
              <thead>
                <tr><th>来源 IP</th><th>命中次数</th><th>不同路径</th><th>工具指纹</th><th>最近命中</th><th>操作</th></tr>
              </thead>
              <tbody>
                <tr v-for="row in decoy.sources" :key="row.ip" :data-testid="`decoy-source-${row.ip}`">
                  <td class="font-mono">{{ row.ip }}</td>
                  <td>{{ row.count }}</td>
                  <td>{{ row.path_count }}</td>
                  <td>{{ (row.user_agents[0] && row.user_agents[0].user_agent) || '—' }}</td>
                  <td>{{ formatTime(row.last_seen) }}</td>
                  <td><button class="button button-text" type="button" @click="traceFromDecoy(row.ip)">溯源</button></td>
                </tr>
              </tbody>
            </table>
          </div>
          <details v-if="decoy.redirect_total" class="decoy-members">
            <summary>查看已引流来源明细</summary>
            <p class="panel-footnote">IPv4：{{ decoy.redirect_members['4'].join(', ') || '无' }}</p>
            <p class="panel-footnote">IPv6：{{ decoy.redirect_members['6'].join(', ') || '无' }}</p>
          </details>
          <p v-if="decoy.errors.length" class="panel-footnote">采集提示：{{ decoy.errors.join('；') }}</p>
        </template>
        <p v-else class="empty-state">尚未读取诱捕层状态。</p>
      </section>
    </section>
  </main>
</template>

<style scoped lang="scss">
.security-page { display: flex; min-width: 0; flex-direction: column; gap: var(--sp-5); color: var(--gray-800); font: var(--fs-base)/1.6 var(--font-sans); }
.security-page *, .security-page *::before, .security-page *::after { box-sizing: border-box; }
.security-page h2, .security-page h3, .security-page h4, .security-page p { margin: 0; }
.security-page svg { flex: 0 0 auto; }
.security-hero { display: flex; min-width: 0; align-items: center; justify-content: space-between; gap: var(--sp-5); padding: var(--sp-5); border: 1px solid var(--brand-100); border-radius: var(--r-xl); background: linear-gradient(115deg, var(--color-white), var(--brand-50) 75%, var(--accent-50)); box-shadow: var(--shadow-2); }
.hero-main { display: flex; min-width: 0; align-items: center; gap: var(--sp-4); }
.shield-art { position: relative; display: grid; width: 64px; height: 72px; flex: 0 0 64px; place-items: center; border-radius: var(--r-xl); background: linear-gradient(145deg, var(--brand-400), var(--accent-500)); box-shadow: var(--shadow-2); }
.shield-art svg { width: 42px; fill: none; stroke: var(--color-white); stroke-linecap: round; stroke-linejoin: round; stroke-width: 4; }
.shield-art svg path:first-child { fill: rgba(255,255,255,.16); }
.shield-spark { position: absolute; top: 8px; right: 8px; color: var(--sev-medium-bg); font-size: 16px; }
.hero-copy { min-width: 0; flex: 1 1 auto; }
.eyebrow { color: var(--brand-600); font: 600 var(--fs-xs)/1.5 var(--font-mono); letter-spacing: .12em; }
.hero-copy h2 { margin: var(--sp-1) 0; color: var(--gray-900); font-size: var(--fs-xl); line-height: 1.4; }
.hero-copy p { color: var(--gray-600); overflow-wrap: anywhere; }
.hero-badges { display: flex; align-items: baseline; flex-wrap: wrap; gap: var(--sp-2) var(--sp-4); margin-top: var(--sp-2); }
.mode-badge { display: inline-flex; align-items: center; gap: var(--sp-1); padding: var(--sp-1) var(--sp-2); border-radius: 99px; color: var(--brand-700); background: var(--brand-100); font-size: var(--fs-xs); font-weight: 600; }
.mode-badge svg { width: 14px; height: 14px; }
.mode-note { color: var(--gray-600); font-size: var(--fs-xs); }
.hero-actions, .policy-actions { display: flex; flex: 0 0 auto; flex-wrap: wrap; align-items: center; gap: var(--sp-2); }
.button { display: inline-flex; min-width: 0; min-height: 44px; align-items: center; justify-content: center; gap: var(--sp-2); padding: var(--sp-2) var(--sp-4); border: 1px solid transparent; border-radius: var(--r-md); font: 600 var(--fs-base)/1.5 var(--font-sans); cursor: pointer; transition: background .15s, border-color .15s; }
.button svg { width: 16px; height: 16px; }
.button-primary { color: var(--color-white); border-color: var(--brand-500); background: var(--brand-500); }
.button-primary:hover:not(:disabled) { background: var(--brand-600); }
.button-secondary { color: var(--gray-700); border-color: var(--gray-200); background: var(--color-white); }
.button-secondary:hover:not(:disabled) { border-color: var(--brand-200); background: var(--brand-50); }
.button-text { padding: var(--sp-2); color: var(--brand-600); background: transparent; }
.button-text:hover { background: var(--brand-50); }
button:disabled { cursor: not-allowed; opacity: .55; }
.decoy-metrics { grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
.decoy-apply { display: flex; flex-wrap: wrap; gap: var(--sp-3); align-items: flex-end; margin-top: var(--sp-4); }
.decoy-apply .filter-field { min-width: 260px; flex: 1 1 260px; }
.decoy-subtitle { margin: var(--sp-5) 0 var(--sp-2); font-size: var(--fs-md); }
.table-scroll { overflow-x: auto; }
.decoy-table { width: 100%; border-collapse: collapse; font-size: var(--fs-sm); }
.decoy-table th, .decoy-table td { padding: var(--sp-2) var(--sp-3); border-bottom: 1px solid var(--gray-200); text-align: left; white-space: nowrap; }
.decoy-table th { color: var(--gray-500); font-weight: 600; }
.decoy-members { margin-top: var(--sp-4); }
.decoy-members summary { cursor: pointer; color: var(--brand-600); }
button:focus-visible, input:focus-visible, select:focus-visible, summary:focus-visible, [role=tabpanel]:focus-visible { outline: 2px solid var(--brand-500); outline-offset: 3px; box-shadow: var(--focus-ring); }
.security-tabs { display: flex; align-items: stretch; gap: var(--sp-2); border-bottom: 1px solid var(--gray-200); }
.security-tab { position: relative; min-width: 0; min-height: 48px; padding: var(--sp-2) var(--sp-5); border: 0; border-radius: var(--r-md) var(--r-md) 0 0; color: var(--gray-600); background: transparent; font: 600 var(--fs-base)/1.5 var(--font-sans); cursor: pointer; }
.security-tab[aria-selected=true] { color: var(--brand-600); background: var(--brand-50); }
.security-tab[aria-selected=true]::after { position: absolute; right: var(--sp-4); bottom: -1px; left: var(--sp-4); height: 3px; border-radius: 3px 3px 0 0; background: var(--brand-500); content: ''; }
.section-content { display: flex; min-width: 0; flex-direction: column; gap: var(--sp-5); }
.metrics-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--sp-4); }
.metric-card { display: flex; min-width: 0; align-items: flex-start; gap: var(--sp-3); padding: var(--sp-5) var(--sp-4); border: 1px solid var(--gray-200); border-radius: var(--r-lg); background: var(--color-white); box-shadow: var(--shadow-1); }
.metric-icon { display: grid; width: 36px; height: 36px; flex: 0 0 36px; place-items: center; border-radius: var(--r-md); }
.metric-icon svg { width: 20px; height: 20px; }
.posture-icon, .time-icon { color: var(--brand-600); background: var(--brand-50); }
.alert-icon { color: var(--sev-high); background: var(--sev-high-bg); }
.source-icon { color: var(--accent-600); background: var(--accent-50); }
.metric-content { display: flex; min-width: 0; flex-direction: column; gap: var(--sp-1); }
.metric-label, .metric-content small { color: var(--gray-600); font-size: var(--fs-xs); }
.metric-value { color: var(--gray-900); font-size: var(--fs-2xl); font-weight: 700; line-height: 1.4; overflow-wrap: anywhere; }
.metric-value-small { font-size: var(--fs-md); }
.tone-healthy { color: var(--accent-600); }
.tone-warning { color: var(--sev-high); }
.tone-unknown { color: var(--gray-500); }
.overview-columns { display: grid; min-width: 0; grid-template-columns: minmax(0, 1.45fr) minmax(0, 1fr); gap: var(--sp-5); align-items: start; }
.panel { min-width: 0; padding: var(--sp-5); border: 1px solid var(--gray-200); border-radius: var(--r-lg); background: var(--color-white); box-shadow: var(--shadow-1); }
.section-heading { display: flex; min-width: 0; align-items: flex-start; justify-content: space-between; gap: var(--sp-4); margin-bottom: var(--sp-4); }
.section-heading h3 { color: var(--gray-900); font-size: var(--fs-lg); font-weight: 600; line-height: 1.5; }
.section-subtitle { margin-top: var(--sp-1) !important; color: var(--gray-600); font-size: var(--fs-xs); }
.state-banner { display: flex; min-width: 0; align-items: center; justify-content: space-between; gap: var(--sp-4); padding: var(--sp-3) var(--sp-4); border-radius: var(--r-md); }
.error-banner { color: var(--sev-severe); border: 1px solid var(--sev-severe); background: var(--sev-severe-bg); }
.state-banner > span { min-width: 0; overflow-wrap: anywhere; }
.state-banner .button { flex: 0 0 auto; }
.runtime-panel .state-banner { margin-bottom: var(--sp-4); }
.runtime-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--sp-5); }
.runtime-metric { display: flex; min-width: 0; flex-direction: column; gap: var(--sp-2); }
.runtime-topline { display: flex; align-items: baseline; justify-content: space-between; gap: var(--sp-2); flex-wrap: wrap; }
.runtime-metric span { color: var(--gray-600); font-size: var(--fs-xs); }
.runtime-metric strong { color: var(--gray-900); font: 600 var(--fs-lg)/1.5 var(--font-sans); }
.runtime-metric small, .panel-footnote { color: var(--gray-500); font-size: var(--fs-xs); }
progress, .progress-placeholder { display: block; width: 100%; height: 8px; overflow: hidden; border: 0; border-radius: 99px; background: var(--gray-100); accent-color: var(--brand-500); }
progress::-webkit-progress-bar { background: var(--gray-100); border-radius: 99px; }
progress::-webkit-progress-value { background: var(--brand-500); border-radius: 99px; }
progress::-moz-progress-bar { background: var(--brand-500); border-radius: 99px; }
.uptime-metric { gap: var(--sp-1); }
.panel-footnote { margin-top: var(--sp-5) !important; }
.source-list { display: flex; flex-direction: column; gap: var(--sp-3); margin: 0; padding: 0; list-style: none; }
.source-list li { display: grid; grid-template-columns: 8px minmax(0, 1fr) auto; align-items: baseline; gap: var(--sp-2); }
.source-dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.source-name { min-width: 0; font-size: var(--fs-base); overflow-wrap: anywhere; }
.state-pill { display: inline-flex; max-width: 100%; align-items: center; justify-content: center; padding: var(--sp-1) var(--sp-2); border-radius: 99px; color: var(--gray-600); background: var(--gray-100); font-size: var(--fs-xs); font-weight: 500; line-height: 1.5; overflow-wrap: anywhere; text-align: center; }
.state-pill.tone-healthy, .state-pill.status-success, .state-pill.status-resolved { color: var(--accent-600); background: var(--accent-50); }
.state-pill.tone-warning, .state-pill.status-warning, .state-pill.status-open { color: var(--sev-high); background: var(--sev-high-bg); }
.state-pill.status-failed { color: var(--sev-severe); background: var(--sev-severe-bg); }
.source-inspection { margin-top: var(--sp-3); }
.empty-state { display: flex; min-height: 144px; align-items: center; justify-content: center; flex-direction: column; gap: var(--sp-2); padding: var(--sp-5) var(--sp-4); color: var(--gray-600); text-align: center; }
.empty-state svg { width: 28px; height: 28px; color: var(--brand-400); }
.empty-state strong { color: var(--gray-700); font-weight: 600; }
.empty-state p { font-size: var(--fs-xs); }
.recent-list, .event-list { margin: 0; padding: 0; list-style: none; }
.recent-list li { display: grid; min-width: 0; grid-template-columns: 3px minmax(0, 1fr) 176px 104px; align-items: center; gap: var(--sp-4); padding: var(--sp-4) 0; border-bottom: 1px solid var(--gray-100); }
.recent-list li:last-child { border-bottom: 0; }
.event-rail { align-self: stretch; min-height: 40px; border-radius: 99px; background: var(--sev-low); }
.event-rail.sev-warning { background: var(--sev-medium); }
.event-rail.sev-high { background: var(--sev-high); }
.event-rail.sev-critical { background: var(--sev-severe); }
.recent-main { min-width: 0; }
.recent-main h4, .event-title-group h4 { font-size: var(--fs-base); font-weight: 600; line-height: 1.6; overflow-wrap: anywhere; }
.recent-main span { color: var(--gray-500); font-size: var(--fs-xs); }
.recent-list time, .event-topline time { color: var(--gray-500); font-size: var(--fs-xs); font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.recent-list time { text-align: right; }
.event-toolbar { display: flex; align-items: flex-end; flex-wrap: wrap; gap: var(--sp-4); margin-bottom: var(--sp-5); padding: var(--sp-4); border: 1px solid var(--gray-100); border-radius: var(--r-md); background: var(--gray-50); }
.filter-field { display: flex; min-width: 0; flex-direction: column; gap: var(--sp-2); }
.filter-field > span { color: var(--gray-600); font-size: var(--fs-xs); }
.filter-field select { min-width: 144px; min-height: 44px; padding: var(--sp-2) var(--sp-3); border: 1px solid var(--gray-200); border-radius: var(--r-md); color: var(--gray-800); background: var(--color-white); font: var(--fs-base)/1.5 var(--font-sans); }
.event-count { margin-left: auto; align-self: flex-end; padding-bottom: var(--sp-3); color: var(--gray-500); font-size: var(--fs-xs); }
.event-item { display: grid; min-width: 0; grid-template-columns: 32px minmax(0, 1fr) 88px 128px; align-items: start; gap: var(--sp-4); padding: var(--sp-5) 0; border-bottom: 1px solid var(--gray-100); }
.event-item:last-child { border-bottom: 0; }
.event-marker { display: grid; width: 32px; height: 32px; place-items: center; border-radius: var(--r-md); color: var(--sev-low); background: var(--sev-low-bg); }
.event-marker svg { width: 18px; height: 18px; }
.event-marker.sev-warning { color: var(--sev-medium); background: var(--sev-medium-bg); }
.event-marker.sev-high { color: var(--sev-high); background: var(--sev-high-bg); }
.event-marker.sev-critical { color: var(--sev-severe); background: var(--sev-severe-bg); }
.event-main { min-width: 0; }
.event-topline { display: grid; grid-template-columns: minmax(0, 1fr) 144px; align-items: baseline; gap: var(--sp-4); }
.event-topline time { text-align: right; }
.event-layer { color: var(--gray-500); font-size: var(--fs-xs); }
.event-summary { margin-top: var(--sp-2) !important; color: var(--gray-600); overflow-wrap: anywhere; }
.event-details { margin-top: var(--sp-2); }
.event-details summary, .policy-explanation summary { width: fit-content; min-height: 44px; padding: var(--sp-2) 0; color: var(--brand-600); cursor: pointer; }
.event-detail-content { padding: var(--sp-4); border-radius: var(--r-md); background: var(--gray-50); }
.event-actor { color: var(--gray-600); font-size: var(--fs-xs); overflow-wrap: anywhere; }
.event-actor code { font-size: var(--fs-xs); }
.evidence-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--sp-3) var(--sp-4); margin: var(--sp-4) 0 0; }
.evidence-grid > div { min-width: 0; }
.evidence-grid dt { color: var(--gray-500); font-size: var(--fs-xs); }
.evidence-grid dd { margin: var(--sp-1) 0 0; color: var(--gray-800); font-size: var(--fs-base); overflow-wrap: anywhere; }
.resolution-note { margin-top: var(--sp-4) !important; color: var(--gray-700); overflow-wrap: anywhere; }
.resolution-note > span { display: block; margin-top: var(--sp-1); color: var(--gray-500); font-size: var(--fs-xs); }
.event-status { justify-self: stretch; margin-top: var(--sp-1); }
.event-actions { display: flex; min-width: 0; justify-content: flex-end; }
.resolve-button { width: 100%; padding-right: var(--sp-2); padding-left: var(--sp-2); }
.action-placeholder { padding: var(--sp-2) 0; color: var(--gray-500); font-size: var(--fs-xs); }
.timeline-footer { display: flex; min-width: 0; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: var(--sp-4); margin-top: var(--sp-4); padding-top: var(--sp-4); border-top: 1px solid var(--gray-200); color: var(--gray-500); font-size: var(--fs-xs); }
.pager { display: flex; align-items: center; gap: var(--sp-3); }
.pager > span { min-width: 44px; color: var(--gray-600); text-align: center; }
.policy-columns { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr); gap: var(--sp-5); align-items: start; }
.permission-note { display: flex; align-items: center; gap: var(--sp-1); flex: 0 0 auto; color: var(--gray-500); font-size: var(--fs-xs); }
.permission-note svg { width: 14px; height: 14px; }
.policy-form { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--sp-5) var(--sp-4); }
.policy-form > label { display: flex; min-width: 0; flex-direction: column; gap: var(--sp-2); }
.policy-form > label > span { color: var(--gray-700); font-size: var(--fs-base); }
.policy-form small { color: var(--gray-500); font-size: var(--fs-xs); }
.input-suffix { display: flex; min-width: 0; align-items: baseline; border: 1px solid var(--gray-200); border-radius: var(--r-md); background: var(--color-white); }
.input-suffix input { width: 100%; min-width: 0; min-height: 44px; padding: var(--sp-2) var(--sp-3); border: 0; border-radius: var(--r-md); color: var(--gray-800); background: transparent; font: var(--fs-base)/1.5 var(--font-sans); }
.input-suffix input:disabled { color: var(--gray-500); background: var(--gray-50); }
.input-suffix em { flex: 0 0 auto; padding-right: var(--sp-3); color: var(--gray-500); font-size: var(--fs-xs); font-style: normal; }
.full-field { grid-column: 1 / -1; }
.policy-form-footer { display: flex; flex-direction: column; gap: var(--sp-4); padding-top: var(--sp-4); border-top: 1px solid var(--gray-100); }
.policy-draft-state { color: var(--gray-600); font-size: var(--fs-xs); }
.policy-actions { justify-content: flex-end; }
.policy-save-feedback { padding: var(--sp-3); border-radius: var(--r-md); color: var(--accent-600); background: var(--accent-50); }
.policy-save-feedback.feedback-error { color: var(--sev-severe); background: var(--sev-severe-bg); overflow-wrap: anywhere; }
.policy-facts { display: flex; flex-direction: column; gap: var(--sp-3); margin: 0; }
.policy-facts > div { display: grid; grid-template-columns: 80px minmax(0, 1fr); gap: var(--sp-4); align-items: baseline; }
.policy-facts dt { color: var(--gray-500); font-size: var(--fs-xs); }
.policy-facts dd { margin: 0; color: var(--gray-700); font-size: var(--fs-base); overflow-wrap: anywhere; }
.policy-updated { margin-top: var(--sp-5) !important; color: var(--gray-500); font-size: var(--fs-xs); overflow-wrap: anywhere; }
.policy-explanation { margin-top: var(--sp-4); padding-top: var(--sp-2); border-top: 1px solid var(--gray-100); }
.policy-explanation p { margin-top: var(--sp-2); color: var(--gray-600); font-size: var(--fs-xs); }

/* ---------- 溯源与响应（全部只读） ---------- */
.trace-form { display: flex; min-width: 0; align-items: flex-end; flex-wrap: wrap; gap: var(--sp-3); }
.trace-ip-field { flex: 1 1 240px; }
.trace-ip-field input { width: 100%; min-height: 44px; padding: var(--sp-2) var(--sp-3); border: 1px solid var(--gray-200); border-radius: var(--r-md); color: var(--gray-800); background: var(--color-white); font: var(--fs-base)/1.5 var(--font-sans); }
.trace-ip-field input[aria-invalid='true'] { border-color: var(--sev-severe); }
.trace-ip-field input:disabled { color: var(--gray-500); background: var(--gray-50); }
.trace-form .button { flex: 0 0 auto; }
.trace-help, .trace-muted { margin-top: var(--sp-2) !important; color: var(--gray-500); font-size: var(--fs-xs); overflow-wrap: anywhere; }
.trace-inline-error { margin-top: var(--sp-3) !important; padding: var(--sp-2) var(--sp-3); border-radius: var(--r-md); color: var(--sev-severe); background: var(--sev-severe-bg); font-size: var(--fs-xs); overflow-wrap: anywhere; }
.trace-readonly { display: flex; align-items: flex-start; gap: var(--sp-2); margin-top: var(--sp-3) !important; padding: var(--sp-2) var(--sp-3); border-radius: var(--r-md); color: var(--brand-700); background: var(--brand-50); font-size: var(--fs-xs); }
.trace-readonly svg { width: 14px; height: 14px; margin-top: 3px; }
.trace-readonly > span { min-width: 0; overflow-wrap: anywhere; }
.trace-query-panel .state-banner, .trace-surface-panel .state-banner, .trace-traffic-panel .state-banner { margin-top: var(--sp-4); }
.trace-result { margin-top: var(--sp-4); padding-top: var(--sp-4); border-top: 1px solid var(--gray-100); }
.trace-result-head { display: flex; min-width: 0; align-items: flex-start; justify-content: space-between; flex-wrap: wrap; gap: var(--sp-4); }
.trace-result-identity { display: flex; min-width: 0; align-items: center; flex-wrap: wrap; gap: var(--sp-3); }
.trace-ip { color: var(--gray-900); font: 600 var(--fs-md)/1.5 var(--font-mono); overflow-wrap: anywhere; }
.trace-risk { display: flex; align-items: center; flex-wrap: wrap; gap: var(--sp-2); }
.trace-risk-score { color: var(--gray-900); font-size: var(--fs-xl); font-weight: 700; line-height: 1.3; }
.trace-risk small { color: var(--gray-500); font-size: var(--fs-xs); }
.trace-risk progress { width: 96px; }
.trace-meta { margin-top: var(--sp-3) !important; color: var(--gray-600); font-size: var(--fs-xs); overflow-wrap: anywhere; }
.trace-protected { display: flex; align-items: center; gap: var(--sp-2); margin-top: var(--sp-3) !important; padding: var(--sp-3) var(--sp-4); border: 1px solid var(--sev-high); border-radius: var(--r-md); color: var(--sev-high); background: var(--sev-high-bg); font-size: var(--fs-base); font-weight: 600; }
.trace-protected svg { width: 16px; height: 16px; }
.trace-columns { display: grid; min-width: 0; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--sp-5); margin-top: var(--sp-5); }
.trace-block { min-width: 0; }
.trace-block h4 { margin-bottom: var(--sp-2); color: var(--gray-900); font-size: var(--fs-base); font-weight: 600; }
.trace-reason-list { display: flex; flex-direction: column; gap: var(--sp-2); margin: 0; padding-left: var(--sp-4); color: var(--gray-700); font-size: var(--fs-base); }
.trace-reason-list li { overflow-wrap: anywhere; }
.trace-basis { margin-top: var(--sp-3) !important; padding: var(--sp-3); border-radius: var(--r-md); color: var(--gray-700); background: var(--gray-50); font-size: var(--fs-xs); overflow-wrap: anywhere; }
.trace-basis strong { display: block; margin-bottom: var(--sp-1); color: var(--gray-800); }
.trace-attribution-note { display: inline-flex; align-items: center; padding: var(--sp-1) var(--sp-2); border-radius: 99px; color: var(--gray-600); background: var(--gray-100); font-size: var(--fs-xs); }
.trace-facts { display: flex; flex-direction: column; gap: var(--sp-2); margin: var(--sp-3) 0 0; }
.trace-facts > div { display: grid; grid-template-columns: 88px minmax(0, 1fr); gap: var(--sp-3); align-items: baseline; }
.trace-facts dt { color: var(--gray-500); font-size: var(--fs-xs); }
.trace-facts dd { margin: 0; color: var(--gray-800); font-size: var(--fs-base); overflow-wrap: anywhere; }
.trace-metric-line { display: flex; align-items: baseline; gap: var(--sp-2); }
.trace-metric-line strong { color: var(--gray-900); font-size: var(--fs-2xl); font-weight: 700; line-height: 1.2; }
.trace-metric-line span { min-width: 0; color: var(--gray-600); font-size: var(--fs-xs); overflow-wrap: anywhere; }
.trace-tag-list { display: flex; flex-wrap: wrap; gap: var(--sp-2); margin: var(--sp-3) 0 0; padding: 0; list-style: none; }
.trace-tag-list li { display: inline-flex; min-width: 0; align-items: baseline; gap: var(--sp-2); padding: var(--sp-1) var(--sp-2); border: 1px solid var(--gray-200); border-radius: var(--r-md); background: var(--gray-50); font-size: var(--fs-xs); }
.trace-tag-list code { overflow-wrap: anywhere; }
.trace-tag-list span { color: var(--gray-500); }
.trace-records { margin-top: var(--sp-5); }
.trace-table-wrap { max-width: 100%; overflow-x: auto; margin-top: var(--sp-3); }
.trace-table { width: 100%; min-width: 560px; border-collapse: collapse; font-size: var(--fs-xs); }
.trace-table th, .trace-table td { padding: var(--sp-2) var(--sp-3); border-bottom: 1px solid var(--gray-100); text-align: left; vertical-align: top; overflow-wrap: anywhere; }
.trace-table th { color: var(--gray-500); font-weight: 600; white-space: nowrap; }
.trace-table td { color: var(--gray-700); }
.trace-table tr.is-public td { background: var(--sev-high-bg); }
.trace-table tr:last-child td { border-bottom: 0; }
.trace-reason-cell { min-width: 160px; }
.trace-endpoint-cell small { display: block; margin-top: var(--sp-1); color: var(--gray-500); }
.trace-diagnostics { margin-top: var(--sp-4); }
.trace-diagnostics summary { width: fit-content; min-height: 44px; padding: var(--sp-2) 0; color: var(--brand-600); cursor: pointer; }
.trace-diagnostic-list { display: flex; flex-direction: column; gap: var(--sp-1); margin: 0; padding-left: var(--sp-4); color: var(--gray-600); font-size: var(--fs-xs); overflow-wrap: anywhere; }
.state-pill.risk-low { color: var(--accent-600); background: var(--accent-50); }
.state-pill.risk-warning { color: var(--sev-medium); background: var(--sev-medium-bg); }
.state-pill.risk-high { color: var(--sev-high); background: var(--sev-high-bg); }
.state-pill.risk-critical { color: var(--sev-severe); background: var(--sev-severe-bg); }
.state-pill.risk-unknown { color: var(--gray-600); background: var(--gray-100); }
.state-pill.status-active { color: var(--accent-600); background: var(--accent-50); }
.state-pill.status-expired, .state-pill.status-released { color: var(--gray-600); background: var(--gray-100); }
.surface-facts, .traffic-facts { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--sp-4); margin: 0; }
.traffic-facts { grid-template-columns: repeat(5, minmax(0, 1fr)); margin-top: var(--sp-4); }
.surface-facts > div, .traffic-facts > div { min-width: 0; padding: var(--sp-3); border: 1px solid var(--gray-100); border-radius: var(--r-md); background: var(--gray-50); }
.surface-facts dt, .traffic-facts dt { color: var(--gray-500); font-size: var(--fs-xs); }
.surface-facts dd, .traffic-facts dd { margin: var(--sp-1) 0 0; color: var(--gray-900); font-size: var(--fs-base); font-weight: 600; overflow-wrap: anywhere; }
.surface-section { margin-top: var(--sp-5); }
.surface-section h4 { color: var(--gray-900); font-size: var(--fs-base); font-weight: 600; }
.surface-subheading { display: flex; min-width: 0; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: var(--sp-3); }
.surface-filter { display: inline-flex; align-items: center; gap: var(--sp-2); min-height: 44px; color: var(--gray-600); font-size: var(--fs-xs); }
.surface-filter input { width: 18px; height: 18px; accent-color: var(--brand-500); }
.surface-app-list { display: flex; flex-direction: column; gap: var(--sp-2); margin: var(--sp-3) 0 0; padding: 0; list-style: none; }
.surface-app-list li { display: grid; min-width: 0; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: var(--sp-1) var(--sp-3); padding: var(--sp-3); border: 1px solid var(--gray-100); border-radius: var(--r-md); }
.surface-app-list li.installed { border-color: var(--brand-100); background: var(--brand-50); }
.app-name { min-width: 0; font-weight: 600; overflow-wrap: anywhere; }
.app-purpose { grid-column: 1; min-width: 0; color: var(--gray-500); font-size: var(--fs-xs); overflow-wrap: anywhere; }
.surface-app-list .state-pill { grid-row: 1 / span 2; grid-column: 2; }
.firewall-family { margin-top: var(--sp-3); }
.firewall-head { display: flex; align-items: baseline; flex-wrap: wrap; gap: var(--sp-2) var(--sp-3); color: var(--gray-600); font-size: var(--fs-xs); }
.firewall-head strong { color: var(--gray-800); font-size: var(--fs-base); }
.firewall-chains { display: flex; flex-direction: column; gap: var(--sp-2); margin: var(--sp-2) 0 0; padding: 0; list-style: none; }
.firewall-chains li { display: flex; min-width: 0; align-items: center; flex-wrap: wrap; gap: var(--sp-2); padding: var(--sp-2) var(--sp-3); border: 1px solid var(--gray-100); border-radius: var(--r-md); font-size: var(--fs-xs); }
.firewall-chains code { overflow-wrap: anywhere; }
.chain-note { min-width: 0; color: var(--gray-500); overflow-wrap: anywhere; }
.traffic-controls { display: flex; align-items: flex-end; flex-wrap: wrap; gap: var(--sp-3); }
.traffic-payload-note { display: flex; align-items: flex-start; gap: var(--sp-2); margin-top: var(--sp-4) !important; padding: var(--sp-3) var(--sp-4); border: 1px solid var(--brand-100); border-radius: var(--r-md); color: var(--brand-700); background: var(--brand-50); font-size: var(--fs-xs); }
.traffic-payload-note svg { width: 14px; height: 14px; margin-top: 3px; }
.traffic-payload-note > span { min-width: 0; overflow-wrap: anywhere; }
.traffic-payload-note.payload-alert { color: var(--sev-severe); border-color: var(--sev-severe); background: var(--sev-severe-bg); }
.traffic-payload-note + .state-banner, .traffic-payload-note + .empty-state { margin-top: var(--sp-4); }
.visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
.spinning { animation: security-spin 1s linear infinite; }
@keyframes security-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spinning { animation: none; } }
@media (max-width: 1100px) { .metrics-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .security-hero { align-items: flex-start; } .hero-actions { flex-direction: column; } .hero-actions .button { width: 100%; } .event-topline { grid-template-columns: 1fr; gap: var(--sp-1); } .event-topline time { text-align: left; } }
@media (max-width: 920px) { .overview-columns, .policy-columns { grid-template-columns: 1fr; } .source-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--sp-4); } .event-item { grid-template-columns: 32px minmax(0, 1fr) 80px 120px; gap: var(--sp-3); } }
@media (max-width: 640px) {
  .security-page, .section-content { gap: var(--sp-4); }
  .security-hero { flex-direction: column; gap: var(--sp-4); padding: var(--sp-4); }
  .shield-art { width: 48px; height: 56px; flex-basis: 48px; border-radius: var(--r-lg); }
  .shield-art svg { width: 34px; }
  .shield-spark { top: var(--sp-1); right: var(--sp-1); font-size: var(--fs-xs); }
  .hero-main { width: 100%; gap: var(--sp-3); }
  .hero-copy h2 { font-size: var(--fs-lg); }
  .hero-copy p { font-size: var(--fs-xs); }
  .hero-actions { width: 100%; flex-direction: row; }
  .hero-actions .button { flex: 1 1 0; width: auto; padding-right: var(--sp-2); padding-left: var(--sp-2); }
  .security-tabs { gap: 0; }
  .security-tab { flex: 1 1 0; padding: var(--sp-2); }
  .metrics-grid { gap: var(--sp-3); }
  .metric-card { flex-direction: column; gap: var(--sp-2); padding: var(--sp-4); }
  .metric-value-small { font-size: var(--fs-base); }
  .panel { padding: var(--sp-4); }
  .section-heading { gap: var(--sp-2); flex-wrap: wrap; }
  .section-heading .button-text { margin-left: auto; }
  .runtime-grid { gap: var(--sp-4); }
  .runtime-metric strong { font-size: var(--fs-md); }
  .source-list { display: flex; }
  .recent-list li { grid-template-columns: 3px minmax(0, 1fr) 88px; gap: var(--sp-2) var(--sp-3); }
  .recent-list .event-rail { grid-row: 1 / span 2; }
  .recent-list .recent-main { grid-column: 2; }
  .recent-list time { grid-row: 2; grid-column: 2 / -1; text-align: left; }
  .recent-list .state-pill { grid-column: 3; grid-row: 1; }
  .event-toolbar { gap: var(--sp-3); padding: var(--sp-3); }
  .filter-field { flex: 1 1 100px; }
  .filter-field select { width: 100%; min-width: 0; }
  .event-count { width: 100%; margin-left: 0; padding-bottom: 0; }
  .event-item { grid-template-columns: 24px minmax(0, 1fr); gap: var(--sp-2) var(--sp-3); padding: var(--sp-4) 0; }
  .event-marker { width: 24px; height: 28px; }
  .event-marker svg { width: 16px; height: 16px; }
  .event-main { grid-column: 2; }
  .event-status { grid-column: 2; justify-self: start; }
  .event-actions { grid-column: 2; justify-content: flex-start; }
  .resolve-button { width: auto; padding-right: var(--sp-4); padding-left: var(--sp-4); }
  .evidence-grid { grid-template-columns: 1fr; }
  .event-detail-content { padding: var(--sp-3); }
  .timeline-footer { align-items: flex-start; }
  .pager { width: 100%; justify-content: space-between; gap: var(--sp-2); }
  .pager .button { padding-right: var(--sp-3); padding-left: var(--sp-3); }
  .policy-form { gap: var(--sp-4); }
  .policy-actions .button { flex: 1 1 100px; padding-right: var(--sp-2); padding-left: var(--sp-2); }
  .state-banner { align-items: flex-start; gap: var(--sp-2); }
}
@media (max-width: 360px) {
  .metrics-grid { grid-template-columns: 1fr; }
  .metric-card { flex-direction: row; }
  .policy-form { grid-template-columns: 1fr; }
  .recent-list li { grid-template-columns: 3px minmax(0, 1fr) 72px; }
  .state-banner { flex-direction: column; }
}
@media (max-width: 1100px) {
  .surface-facts { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .traffic-facts { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 920px) {
  .trace-columns { grid-template-columns: 1fr; }
  .traffic-facts { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 640px) {
  .trace-form { align-items: stretch; }
  .trace-ip-field { flex: 1 1 100%; }
  .trace-form .button { width: 100%; }
  .surface-facts, .traffic-facts { grid-template-columns: 1fr; }
  .trace-result { padding-top: var(--sp-3); }
  .trace-table { min-width: 520px; }
  .traffic-controls { width: 100%; }
  .traffic-controls .filter-field { flex: 1 1 140px; }
  .traffic-controls .button { flex: 1 1 100%; }
  .trace-query-panel .state-banner, .trace-surface-panel .state-banner, .trace-traffic-panel .state-banner { align-items: flex-start; }
}
@media (max-width: 360px) {
  .trace-table { min-width: 460px; }
  .trace-risk { width: 100%; }
  .trace-risk progress { width: 100%; }
  .surface-app-list li { grid-template-columns: minmax(0, 1fr); }
  .surface-app-list .state-pill { grid-row: auto; grid-column: 1; justify-self: start; }
}
</style>
