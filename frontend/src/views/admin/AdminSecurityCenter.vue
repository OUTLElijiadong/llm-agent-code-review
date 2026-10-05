<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { Aim, CircleCheck, Clock, DataAnalysis, Lock, Refresh, WarningFilled } from '@element-plus/icons-vue'
import {
  getSecurityCenterEvents,
  getSecurityCenterOverview,
  runSecurityMonitor,
  updateSecurityMonitorPolicy,
  type SecurityCenterEvent,
  type SecurityCenterEventPage,
  type SecurityCenterOverview,
  type SecurityMonitorPolicy,
  type AutomaticBlockingSnapshot,
} from '@/api/adminSecurityCenter'
import { getSystemStatus, type SystemStatus } from '@/api/adminOverview'
import { resolveAlert } from '@/api/adminGovernance'
import AutomaticBlockingPanel from '@/components/security/AutomaticBlockingPanel.vue'

const overview = ref<SecurityCenterOverview | null>(null)
const eventPage = ref<SecurityCenterEventPage | null>(null)
const systemStatus = ref<SystemStatus | null>(null)
const overviewLoading = ref(true)
const eventsLoading = ref(true)
const serverLoading = ref(true)
const overviewError = ref('')
const eventsError = ref('')
const serverError = ref('')
const runLoading = ref(false)
const saveLoading = ref(false)
const resolvingAlertId = ref<number | null>(null)
const hours = ref(24)
const page = ref(1)
const clock = ref(Date.now())
const blockingPanel = ref<InstanceType<typeof AutomaticBlockingPanel> | null>(null)
const blockingStatus = reactive<{ snapshot: AutomaticBlockingSnapshot | null; loading: boolean; error: string }>({ snapshot: null, loading: true, error: '' })
let eventRequestGeneration = 0
let clockTimer: ReturnType<typeof setInterval> | undefined
const policyDraft = reactive({
  ssh_failed_threshold: 20,
  ssh_window_hours: 1,
  nginx_failure_threshold: 20,
  nginx_window_hours: 1,
})

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
const sourceIssueLabels = computed(() => (overview.value?.monitoring.sources ?? [])
  .filter((source) => source.status === 'failed' || source.status === 'degraded')
  .map((source) => `${source.label}（${source.status === 'failed' ? '失败' : '降级'}）`))
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
  if (!blockingStatus.snapshot.verified) return '自动封禁：执行状态待核验'
  return blockingStatus.snapshot.enabled ? '自动封禁：已启用' : '自动封禁：关闭'
})
const securityModeLabel = computed(() => blockingStatus.snapshot?.available && blockingStatus.snapshot.verified && blockingStatus.snapshot.enabled
  && !blockingStatus.loading && !blockingStatus.error ? '监控与临时自动封禁' : '监控与告警')

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
    warning: '部分异常', unknown: '未知',
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
    failed_source_codes: '失败来源', degraded_source_codes: '降级来源',
  }
  return Object.entries(event.evidence_summary || {}).map(([key, value]) => [
    labels[key] || key,
    typeof value === 'object' && value !== null ? JSON.stringify(value) : String(value),
  ])
}

function syncPolicy(policy: SecurityMonitorPolicy): void {
  policyDraft.ssh_failed_threshold = policy.ssh_failed_threshold
  policyDraft.ssh_window_hours = policy.ssh_window_hours
  policyDraft.nginx_failure_threshold = policy.nginx_failure_threshold
  policyDraft.nginx_window_hours = policy.nginx_window_hours
}

async function loadOverview(): Promise<void> {
  overviewLoading.value = true
  try {
    overview.value = await getSecurityCenterOverview()
    syncPolicy(overview.value.policy)
    overviewError.value = ''
  } catch (error) {
    overviewError.value = getErrorMessage(error)
  } finally {
    overviewLoading.value = false
  }
}

async function loadEvents(): Promise<void> {
  const requestGeneration = ++eventRequestGeneration
  const requestedHours = hours.value
  const requestedPage = page.value
  eventsLoading.value = true
  eventsError.value = ''
  eventPage.value = null
  try {
    const result = await getSecurityCenterEvents(requestedHours, requestedPage, 20)
    if (requestGeneration === eventRequestGeneration) eventPage.value = result
  } catch (error) {
    if (requestGeneration === eventRequestGeneration) eventsError.value = getErrorMessage(error)
  } finally {
    if (requestGeneration === eventRequestGeneration) eventsLoading.value = false
  }
}

async function loadServer(): Promise<void> {
  serverLoading.value = true
  try {
    systemStatus.value = await getSystemStatus()
    serverError.value = ''
  } catch (error) {
    serverError.value = getErrorMessage(error)
  } finally {
    serverLoading.value = false
  }
}

async function refreshAll(refreshBlocking = true): Promise<void> {
  await Promise.allSettled([loadOverview(), loadEvents(), loadServer(), ...(refreshBlocking && blockingPanel.value ? [blockingPanel.value.refresh()] : [])])
}

async function refreshEvents(): Promise<void> {
  page.value = 1
  await loadEvents()
}

async function triggerMonitor(): Promise<void> {
  if (runLoading.value) return
  runLoading.value = true
  try {
    const result = await runSecurityMonitor()
    if (result.success) ElMessage.success(`巡检完成，新增 ${result.created_alerts.length} 条告警。`)
    else ElMessage.warning(`巡检存在数据源异常（${result.errors.length} 项），请查看数据源覆盖状态。`)
    await refreshAll()
  } catch (error) {
    ElMessage.error(`巡检未完成：${getErrorMessage(error)}`)
  } finally {
    runLoading.value = false
  }
}

async function savePolicy(): Promise<void> {
  if (!overview.value || saveLoading.value) return
  saveLoading.value = true
  try {
    const updated = await updateSecurityMonitorPolicy({ ...policyDraft })
    overview.value.policy = updated
    syncPolicy(updated)
    ElMessage.success('监控灵敏度已更新，并写入操作审计。')
    await loadEvents()
  } catch (error) {
    ElMessage.error(`策略未保存：${getErrorMessage(error)}`)
  } finally {
    saveLoading.value = false
  }
}

async function changePage(next: number): Promise<void> {
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
    await Promise.allSettled([loadOverview(), loadEvents()])
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

onUnmounted(() => {
  if (clockTimer) clearInterval(clockTimer)
})
</script>

<template>
  <main class="security-page" aria-labelledby="security-title">
    <section class="security-hero">
      <div class="hero-main">
        <div class="shield-art" aria-hidden="true">
          <svg viewBox="0 0 72 80" role="presentation">
            <path d="M36 4 63 14v21c0 18-11 31-27 40C20 66 9 53 9 35V14L36 4Z" />
            <path d="m24 39 8 8 17-19" />
          </svg>
          <span class="shield-spark">✦</span>
        </div>
        <div class="hero-copy">
          <div class="eyebrow">PRISM · SECURITY OPERATIONS</div>
          <h2 id="security-title">小菱安全中心</h2>
          <p>集中查看服务器运行、被动安全监控、告警和策略变更。</p>
          <div class="hero-badges">
            <span class="mode-badge"><Lock /> {{ securityModeLabel }}</span>
            <span class="mode-note">{{ blockingLabel }} · 反击操作未启用</span>
          </div>
        </div>
      </div>
      <div class="hero-actions">
        <button class="button button-secondary" type="button" :disabled="overviewLoading || eventsLoading || serverLoading || blockingStatus.loading" @click="refreshAll()">
          <Refresh :class="{ spinning: overviewLoading || eventsLoading || serverLoading }" />刷新状态
        </button>
        <button class="button button-primary" type="button" :disabled="runLoading || overviewLoading" @click="triggerMonitor">
          <Aim />{{ runLoading ? '巡检中…' : '立即巡检' }}
        </button>
      </div>
    </section>

    <section class="scope-note" role="note">
      <WarningFilled />
      <p><strong>证据边界：</strong>报告覆盖的入口不同：一份称公网 Web 层未取得管理员权限，另两份描述经主机运维凭据和容器密钥链进入管理环境。材料未附原始服务器日志供本次独立复核，不能据此确认第三方已入侵生产或 Web RBAC 被绕过。本页展示平台实际记录；事件时间是记录/采集时间，不代表攻击发生时间。规则命中和入侵成功分别记录，封禁是否生效以自动封禁执行回执为准。</p>
      <p><strong>日志覆盖范围：</strong>目前显示已接入的 SSH/Nginx 规则告警与采集回执、登录审计、角色/权限及高影响配置变更；这不代表完整的 SSH/Nginx 原始日志。没有审计记录的成功只读 API 请求不会出现在此时间线；没有记录不能证明该请求未发生。</p>
    </section>

    <p v-if="overviewError" class="state-banner error-banner" role="alert">安全状态读取失败：{{ overviewError }}。现有统计可能是上次结果。</p>
    <p v-if="serverError" class="state-banner error-banner" role="alert">运行环境资源读取失败：{{ serverError }}。</p>

    <section class="metrics-grid" aria-label="安全状态概览" :aria-busy="overviewLoading || serverLoading">
      <article class="metric-card posture-card">
        <div class="metric-icon posture-icon"><DataAnalysis /></div>
        <div class="metric-content">
          <span class="metric-label">监控状态</span>
          <strong v-if="!overviewLoading && overview" class="metric-value" :class="`tone-${monitoringTone}`">{{ monitoringLabel }}</strong>
          <strong v-else class="metric-value metric-unknown">{{ overviewLoading ? '读取中…' : '未知' }}</strong>
          <small>{{ overview?.monitoring.enabled ? `${overview.monitoring.schedule_label}巡检` : '未确认调度是否启用' }}</small>
        </div>
        <span class="metric-glow"></span>
      </article>
      <article class="metric-card">
        <div class="metric-icon alert-icon"><WarningFilled /></div>
        <div class="metric-content">
          <span class="metric-label">近 24 小时待处理告警</span>
          <strong class="metric-value">{{ overviewLoading || !overview ? '—' : overview.open_alerts_24h }}</strong>
          <small>当前开放 {{ overview?.open_alerts_total ?? '—' }} 条</small>
        </div>
      </article>
      <article class="metric-card">
        <div class="metric-icon source-icon"><CircleCheck /></div>
        <div class="metric-content">
          <span class="metric-label">数据源覆盖</span>
          <strong class="metric-value metric-value-small">{{ overviewLoading || !overview ? '读取中…' : coverageLabel }}</strong>
          <small v-if="sourceIssueLabels.length">最近记录异常来源：{{ sourceIssueLabels.join('、') }}</small>
          <small v-else>{{ overview?.monitoring.sources.length ?? '—' }} 个被动采集来源</small>
        </div>
      </article>
      <article class="metric-card">
        <div class="metric-icon time-icon"><Clock /></div>
        <div class="metric-content">
          <span class="metric-label">最近巡检</span>
          <strong class="metric-value metric-value-small">{{ overviewLoading ? '读取中…' : overview ? lastRunLabel : '状态未知' }}</strong>
          <small>{{ overview?.monitoring.last_run.failed_sources ? `${overview.monitoring.last_run.failed_sources} 个来源未成功` : '以最近持久化运行记录为准' }}</small>
        </div>
      </article>
    </section>

    <section class="runtime-section">
      <div class="section-heading">
        <div>
          <div class="eyebrow">RUNTIME SNAPSHOT</div>
          <h3>运行环境资源</h3>
        </div>
        <span v-if="systemStatus?.collected_at" class="muted">采集于 {{ formatTime(systemStatus.collected_at) }}</span>
      </div>
      <div class="runtime-grid">
        <div class="runtime-metric">
          <span>CPU 使用率</span>
          <strong>{{ serverLoading ? '读取中…' : systemStatus?.available ? `${systemStatus.cpu_percent ?? '未知'}%` : '不可用' }}</strong>
        </div>
        <div class="runtime-metric">
          <span>内存使用率</span>
          <strong>{{ serverLoading ? '读取中…' : systemStatus?.available ? `${systemStatus.memory_percent ?? '未知'}%` : '不可用' }}</strong>
        </div>
        <div class="runtime-metric">
          <span>磁盘使用率</span>
          <strong>{{ serverLoading ? '读取中…' : systemStatus?.available ? `${systemStatus.disk_percent ?? '未知'}%` : '不可用' }}</strong>
          <small v-if="systemStatus?.available">{{ systemStatus.disk_used_gb ?? '未知' }} / {{ systemStatus.disk_total_gb ?? '未知' }} GB</small>
        </div>
        <div class="runtime-metric">
          <span>服务器运行时长</span>
          <strong>{{ serverLoading ? '读取中…' : systemStatus?.uptime_seconds ? `${Math.floor(systemStatus.uptime_seconds / 86400)} 天` : '未知' }}</strong>
          <small>当前采集进程视角</small>
        </div>
      </div>
      <p class="runtime-footnote">资源值来自后端运行环境采集，不能据此单独推断云主机全盘容量、磁盘增长速度或证书状态。</p>
    </section>

    <AutomaticBlockingPanel
      ref="blockingPanel"
      @status="Object.assign(blockingStatus, $event)"
      @changed="loadEvents()"
    />

    <section class="content-grid">
      <section class="timeline-panel panel">
        <div class="section-heading timeline-heading">
          <div>
            <div class="eyebrow">RECORDED EVENTS</div>
            <h3>安全事件与监控巡检记录</h3>
            <p class="section-subtitle">按系统记录时间排序；采集回执只说明数据读取结果。</p>
          </div>
          <label class="range-select">
            <span class="sr-only">事件回看范围</span>
            <select v-model.number="hours" @change="refreshEvents">
              <option :value="24">近 24 小时</option>
              <option :value="168">近 7 天</option>
              <option :value="720">近 30 天</option>
            </select>
          </label>
        </div>

        <p v-if="eventsError" class="state-banner error-banner" role="alert">事件记录读取失败：{{ eventsError }}</p>
        <div v-if="eventsLoading && !eventPage" class="timeline-empty" role="status">正在读取安全事件记录…</div>
        <div v-else-if="!eventsLoading && !eventsError && !eventPage?.items.length" class="timeline-empty">
          <span class="empty-shield"><Lock /></span>
          <strong>当前范围内没有已记录事件</strong>
          <p>这表示没有匹配的持久化记录，不等于已证明没有攻击。</p>
        </div>
        <ol v-else-if="eventPage" class="event-list" :aria-busy="eventsLoading">
          <li v-for="event in eventPage.items" :key="event.id" class="event-item">
            <span class="event-rail" :class="`sev-${event.severity}`"></span>
            <div class="event-marker" :class="`sev-${event.severity}`">
              <WarningFilled v-if="event.severity === 'critical' || event.severity === 'high' || event.severity === 'warning'" />
              <CircleCheck v-else />
            </div>
            <div class="event-main">
              <div class="event-topline">
                <div class="event-title-group">
                  <span class="event-layer">{{ event.layer }}</span>
                  <h4>{{ event.title }}</h4>
                </div>
                <time :datetime="event.recorded_at || undefined">{{ formatTime(event.recorded_at) }}</time>
              </div>
              <p class="event-summary">{{ event.summary }}</p>
              <div class="event-meta">
                <span>记录方：{{ event.actor }}</span>
                <span>状态：{{ eventStatusLabel(event.status) }}</span>
                <span v-if="event.action_code">动作：{{ event.action_code }}</span>
              </div>
              <dl v-if="evidenceEntries(event).length" class="evidence-grid">
                <div v-for="[label, value] in evidenceEntries(event)" :key="label">
                  <dt>{{ label }}</dt><dd>{{ value }}</dd>
                </div>
              </dl>
              <p v-if="event.resolution?.note" class="resolution-note">处置备注：{{ event.resolution.note }} · {{ event.resolution.resolved_by_name || '管理员' }} · {{ formatTime(event.resolution.resolved_at) }}</p>
              <button
                v-if="event.event_type === 'alert' && event.status === 'open' && event.alert_id"
                class="resolve-button"
                type="button"
                :disabled="resolvingAlertId !== null"
                @click="resolveSecurityAlert(event)"
              >{{ resolvingAlertId === event.alert_id ? '保存中…' : '标记为已处理' }}</button>
            </div>
            <span class="event-status" :class="`status-${event.status}`">{{ eventStatusLabel(event.status) }}</span>
          </li>
        </ol>

        <div v-if="eventPage && eventPage.total > 0" class="timeline-footer">
          <span>{{ eventPage.truncated ? `至少 ${eventPage.total} 条，数据源达到单类读取上限；请缩小时间范围。` : `共 ${eventPage.total} 条记录` }}</span>
          <div class="pager">
            <button type="button" :disabled="page <= 1 || eventsLoading" @click="changePage(page - 1)">上一页</button>
            <span>{{ page }} / {{ Math.max(eventPage.pages, 1) }}</span>
            <button type="button" :disabled="page >= eventPage.pages || eventsLoading" @click="changePage(page + 1)">下一页</button>
          </div>
        </div>
      </section>

      <aside class="side-column">
        <section class="policy-panel panel" aria-labelledby="policy-title">
          <div class="section-heading">
            <div>
              <div class="eyebrow">MONITORING POLICY</div>
              <h3 id="policy-title">监控灵敏度</h3>
            </div>
            <Lock class="policy-lock" aria-label="仅最高管理员可调整" />
          </div>
          <p class="section-subtitle">此处只调整告警检测；自动封禁由上方独立策略管理。</p>
          <div v-if="overviewLoading && !overview" class="form-state">正在读取当前策略…</div>
          <form v-else class="policy-form" @submit.prevent="savePolicy">
            <label>
              <span>SSH 登录失败阈值</span>
              <div class="input-suffix"><input v-model.number="policyDraft.ssh_failed_threshold" type="number" min="1" :max="overview?.policy.ssh_failed_threshold || 5000" required><em>次</em></div>
            </label>
            <label>
              <span>SSH 观察窗口</span>
              <div class="input-suffix"><input v-model.number="policyDraft.ssh_window_hours" type="number" :min="overview?.policy.ssh_window_hours || 1" max="24" required><em>小时</em></div>
            </label>
            <label>
              <span>Nginx 异常请求阈值</span>
              <div class="input-suffix"><input v-model.number="policyDraft.nginx_failure_threshold" type="number" min="1" :max="overview?.policy.nginx_failure_threshold || 5000" required><em>次</em></div>
            </label>
            <label>
              <span>Nginx 观察窗口</span>
              <div class="input-suffix"><input v-model.number="policyDraft.nginx_window_hours" type="number" :min="overview?.policy.nginx_window_hours || 1" max="24" required><em>小时</em></div>
            </label>
            <div class="policy-fixed full-field">
              <span><CircleCheck /> 数据采集：只读</span>
              <span><Lock /> {{ blockingLabel }}</span>
              <span><Lock /> 反击操作：关闭</span>
              <span><WarningFilled /> 推送门槛：{{ popupSeverityLabel }}</span>
            </div>
            <button class="button button-primary save-policy full-field" type="submit" :disabled="saveLoading || !overview">
              {{ saveLoading ? '保存中…' : '保存并记录变更' }}
            </button>
          </form>
          <p v-if="overview?.policy.updated_at" class="policy-updated">最近由 {{ overview.policy.updated_by || '管理员' }} 调整 · {{ formatTime(overview.policy.updated_at) }}</p>
          <p v-else-if="overview" class="policy-updated">当前配置来源：{{ overview.policy.source === 'environment' ? '部署默认值' : '安全回退值' }}</p>
        </section>

        <section class="capabilities-panel panel">
          <div class="section-heading">
            <div><div class="eyebrow">DEFENSE CAPABILITIES</div><h3>当前防御边界</h3></div>
          </div>
          <ul class="capability-list">
            <li><CircleCheck class="capability-on" /><span><strong>日志采集</strong><small>SSH、Nginx、数据库和备份状态</small></span><b>已配置</b></li>
            <li><CircleCheck class="capability-on" /><span><strong>规则告警</strong><small>告警落库；达到通知级别时向管理员弹出提醒</small></span><b>已配置</b></li>
            <li><Aim class="capability-muted" /><span><strong>来源追踪</strong><small>保留日志中的来源信息；归属信息仅作线索</small></span><b>有限</b></li>
            <li><Lock class="capability-muted" /><span><strong>临时自动封禁</strong><small>规则最长 15 分钟，小菱研判最多 2 分钟；以回执确认生效</small></span><b>{{ blockingLabel.replace('自动封禁：', '') }}</b></li>
            <li><Lock class="capability-muted" /><span><strong>反击操作</strong><small>当前未启用</small></span><b>未启用</b></li>
          </ul>
        </section>
      </aside>
    </section>
  </main>
</template>

<style scoped lang="scss">
.security-page { display: flex; flex-direction: column; gap: 20px; color: var(--gray-800); }
.security-hero { position: relative; display: flex; align-items: center; justify-content: space-between; gap: 24px; min-height: 178px; padding: 28px 32px; overflow: hidden; border: 1px solid #dedcff; border-radius: 22px; background: radial-gradient(circle at 91% 10%, rgba(120,116,255,.17), transparent 28%), linear-gradient(120deg, #fff 0%, #f6f5ff 54%, #eef9fb 100%); box-shadow: 0 12px 30px rgba(54, 48, 135, .07); }
.hero-main { display: flex; align-items: center; gap: 22px; min-width: 0; }
.shield-art { position: relative; display: grid; width: 88px; height: 96px; flex: 0 0 88px; place-items: center; border-radius: 25px; background: linear-gradient(145deg, #7167f2, #37b8cb); box-shadow: 0 10px 22px rgba(91, 88, 232, .25); }
.shield-art svg { width: 55px; fill: none; stroke: #fff; stroke-linecap: round; stroke-linejoin: round; stroke-width: 4; }
.shield-art svg path:first-child { fill: rgba(255,255,255,.17); stroke: rgba(255,255,255,.85); }
.shield-spark { position: absolute; top: 13px; right: 12px; color: #ffe794; font-size: 17px; }
.hero-copy { min-width: 0; }
.eyebrow { color: var(--brand-500); font: 700 10px/1.4 var(--font-mono); letter-spacing: .16em; }
.hero-copy h2 { margin: 5px 0 4px; color: var(--gray-900); font: 700 28px/1.25 var(--font-display); }
.hero-copy p { margin: 0; color: var(--gray-600); font-size: 14px; }
.hero-badges { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin-top: 14px; }
.mode-badge { display: inline-flex; align-items: center; gap: 6px; padding: 5px 10px; border: 1px solid #c8e7e7; border-radius: 999px; color: #187b82; background: #e9fbfa; font-size: 12px; font-weight: 700; }
.mode-badge svg { width: 14px; height: 14px; }
.mode-note { color: var(--gray-500); font-size: 12px; }
.hero-actions { display: flex; flex: 0 0 auto; gap: 10px; }
.button { display: inline-flex; min-height: 40px; align-items: center; justify-content: center; gap: 8px; padding: 0 15px; border: 1px solid transparent; border-radius: 11px; font: 600 13px/1 var(--font-sans); cursor: pointer; transition: transform .16s ease, box-shadow .16s ease, background .16s ease; }
.button:focus-visible, select:focus-visible, input:focus-visible, .pager button:focus-visible { outline: 3px solid rgba(91,88,232,.28); outline-offset: 2px; }
.button:disabled { cursor: not-allowed; opacity: .58; }
.button:not(:disabled):hover { transform: translateY(-1px); }
.button svg { width: 16px; height: 16px; }
.button-primary { color: white; background: linear-gradient(135deg, var(--brand-500), #766cf0); box-shadow: 0 5px 12px rgba(91,88,232,.18); }
.button-secondary { color: var(--gray-700); border-color: var(--gray-200); background: rgba(255,255,255,.8); }
.spinning { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.scope-note { display: grid; grid-template-columns: 17px minmax(0, 1fr); align-items: start; gap: 7px 10px; padding: 13px 16px; border: 1px solid #f1dfb9; border-radius: 12px; color: #745a24; background: #fff9eb; }
.scope-note svg { grid-row: 1 / span 2; width: 17px; margin-top: 2px; color: #c28a21; }
.scope-note p { grid-column: 2; margin: 0; font-size: 12px; line-height: 1.7; }
.scope-note strong { color: #66501f; }
.state-banner { margin: 0; padding: 11px 14px; border-radius: 10px; font-size: 13px; }
.error-banner { border: 1px solid #f5c7cf; color: #a4374b; background: #fff4f5; }
.metrics-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
.metric-card { position: relative; display: flex; min-width: 0; min-height: 112px; align-items: flex-start; gap: 13px; padding: 18px; overflow: hidden; border: 1px solid var(--gray-200); border-radius: 16px; background: #fff; box-shadow: 0 4px 14px rgba(26,35,66,.035); }
.metric-icon { display: grid; width: 38px; height: 38px; flex: 0 0 38px; place-items: center; border-radius: 12px; background: #efeeff; color: var(--brand-500); }
.metric-icon svg { width: 19px; height: 19px; }
.alert-icon { color: #bd613f; background: #fff0e9; }
.source-icon { color: #15817e; background: #e8f8f5; }
.time-icon { color: #6a76bb; background: #eff1ff; }
.metric-content { display: flex; min-width: 0; flex-direction: column; gap: 4px; }
.metric-label { color: var(--gray-500); font-size: 12px; }
.metric-value { overflow: hidden; color: var(--gray-900); font: 700 20px/1.35 var(--font-display); text-overflow: ellipsis; white-space: nowrap; }
.metric-value-small { font-size: 14px; line-height: 1.6; white-space: normal; }
.metric-content small { color: var(--gray-500); font-size: 11px; }
.tone-healthy { color: #1c8c70; }
.tone-warning { color: #ba7924; }
.tone-unknown, .metric-unknown { color: var(--gray-500); }
.metric-glow { position: absolute; top: -26px; right: -20px; width: 76px; height: 76px; border-radius: 50%; background: rgba(94,210,180,.13); filter: blur(2px); }
.runtime-section, .panel { border: 1px solid var(--gray-200); border-radius: 17px; background: #fff; box-shadow: 0 4px 16px rgba(26,35,66,.035); }
.runtime-section { padding: 20px; }
.section-heading { display: flex; align-items: center; justify-content: space-between; gap: 15px; }
.section-heading h3 { margin: 4px 0 0; color: var(--gray-900); font-size: 17px; line-height: 1.4; }
.muted { color: var(--gray-500); font-size: 11px; }
.runtime-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin-top: 18px; border: 1px solid var(--gray-100); border-radius: 12px; background: var(--gray-50); }
.runtime-metric { display: flex; min-width: 0; flex-direction: column; gap: 4px; padding: 14px 16px; border-right: 1px solid var(--gray-200); }
.runtime-metric:last-child { border: 0; }
.runtime-metric span { color: var(--gray-500); font-size: 12px; }
.runtime-metric strong { color: var(--gray-900); font: 700 17px/1.4 var(--font-display); overflow-wrap: anywhere; }
.runtime-metric small, .runtime-footnote { color: var(--gray-500); font-size: 10px; }
.runtime-footnote { margin: 11px 0 0; line-height: 1.6; }
.content-grid { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(300px, .9fr); align-items: start; gap: 16px; }
.panel { min-width: 0; padding: 20px; }
.timeline-heading { align-items: flex-start; margin-bottom: 8px; }
.section-subtitle { margin: 6px 0 0; color: var(--gray-500); font-size: 11px; line-height: 1.6; }
.range-select select, .policy-form select { min-height: 36px; padding: 0 30px 0 11px; border: 1px solid var(--gray-200); border-radius: 9px; color: var(--gray-700); background: white; font: 500 12px var(--font-sans); }
.timeline-empty { display: flex; min-height: 190px; flex-direction: column; align-items: center; justify-content: center; gap: 6px; color: var(--gray-500); text-align: center; }
.timeline-empty strong { color: var(--gray-700); font-size: 14px; }
.timeline-empty p { margin: 0; font-size: 11px; }
.empty-shield { display: grid; width: 44px; height: 44px; margin-bottom: 3px; place-items: center; border-radius: 15px; color: var(--brand-500); background: var(--brand-50); }
.event-list { display: flex; flex-direction: column; gap: 0; margin: 12px 0 0; padding: 0; list-style: none; }
.event-item { position: relative; display: grid; grid-template-columns: 3px 28px minmax(0, 1fr) auto; align-items: start; gap: 11px; padding: 16px 0; border-top: 1px solid var(--gray-100); }
.event-rail { width: 3px; height: 100%; min-height: 48px; border-radius: 3px; background: var(--brand-300); }
.event-rail.sev-critical, .event-marker.sev-critical { color: #c63853; background: #fae5e9; }
.event-rail.sev-high, .event-marker.sev-high { color: #bd613f; background: #fff0e9; }
.event-rail.sev-warning, .event-marker.sev-warning { color: #ad7622; background: #fff5dc; }
.event-marker { display: grid; width: 27px; height: 27px; place-items: center; border-radius: 9px; color: var(--brand-500); background: var(--brand-50); }
.event-marker svg { width: 15px; height: 15px; }
.event-main { min-width: 0; }
.event-topline { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
.event-title-group { min-width: 0; }
.event-layer { display: inline-block; margin-bottom: 3px; color: var(--brand-600); font-size: 10px; font-weight: 700; }
.event-title-group h4 { margin: 0; color: var(--gray-800); font-size: 13px; line-height: 1.55; overflow-wrap: anywhere; }
.event-topline time { flex: 0 0 auto; color: var(--gray-500); font: 10px/1.5 var(--font-mono); text-align: right; }
.event-summary { margin: 5px 0; color: var(--gray-600); font-size: 11px; line-height: 1.6; }
.event-meta { display: flex; flex-wrap: wrap; gap: 6px 12px; color: var(--gray-500); font-size: 10px; }
.evidence-grid { display: flex; flex-wrap: wrap; gap: 6px; margin: 9px 0 0; }
.evidence-grid div { display: flex; gap: 5px; padding: 4px 7px; border-radius: 7px; background: var(--gray-50); font-size: 10px; }
.evidence-grid dt { color: var(--gray-500); }
.evidence-grid dd { margin: 0; color: var(--gray-700); font-family: var(--font-mono); overflow-wrap: anywhere; }
.event-status { align-self: start; padding: 4px 7px; border: 1px solid var(--gray-200); border-radius: 999px; color: var(--gray-600); background: var(--gray-50); font-size: 10px; white-space: nowrap; }
.status-open, .status-failed { border-color: #f1c7ce; color: #a93d52; background: #fff4f5; }
.status-running { border-color: #d5d1ff; color: var(--brand-600); background: var(--brand-50); }
.resolution-note { margin: 8px 0 0; padding: 8px 10px; border-left: 3px solid #7bb99b; border-radius: 4px; color: var(--gray-600); background: #f2faf5; font-size: 10px; line-height: 1.6; }
.resolve-button { min-height: 30px; margin-top: 8px; padding: 0 10px; border: 1px solid #d8d5ff; border-radius: 8px; color: var(--brand-600); background: #f7f6ff; font: 600 10px var(--font-sans); cursor: pointer; }
.resolve-button:disabled { cursor: wait; opacity: .6; }
.timeline-footer { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 10px; padding-top: 12px; border-top: 1px solid var(--gray-100); color: var(--gray-500); font-size: 10px; }
.pager { display: flex; align-items: center; gap: 8px; color: var(--gray-600); }
.pager button { min-height: 30px; padding: 0 8px; border: 1px solid var(--gray-200); border-radius: 7px; color: var(--gray-700); background: #fff; font: 11px var(--font-sans); cursor: pointer; }
.pager button:disabled { cursor: not-allowed; opacity: .45; }
.side-column { display: flex; min-width: 0; flex-direction: column; gap: 16px; }
.policy-lock { width: 17px; color: var(--brand-500); }
.policy-form { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 16px; }
.policy-form label { display: flex; min-width: 0; flex-direction: column; gap: 6px; color: var(--gray-600); font-size: 11px; }
.input-suffix { display: flex; min-width: 0; align-items: center; border: 1px solid var(--gray-200); border-radius: 9px; background: #fff; }
.input-suffix:focus-within { border-color: var(--brand-300); box-shadow: 0 0 0 3px rgba(91,88,232,.1); }
.input-suffix input { width: 100%; min-width: 0; height: 36px; padding: 0 8px 0 10px; border: 0; outline: 0; color: var(--gray-900); background: transparent; font: 600 13px var(--font-mono); }
.input-suffix input:focus-visible { outline: 0; }
.input-suffix em { flex: 0 0 auto; padding: 0 9px; color: var(--gray-500); font-size: 10px; font-style: normal; }
.full-field { grid-column: 1 / -1; }
.policy-form .full-field select { width: 100%; }
.policy-fixed { display: flex; flex-wrap: wrap; gap: 6px; }
.policy-fixed span { display: inline-flex; align-items: center; gap: 4px; padding: 5px 7px; border-radius: 7px; color: var(--gray-600); background: var(--gray-50); font-size: 10px; }
.policy-fixed svg { width: 12px; height: 12px; color: #218f7c; }
.policy-fixed span:not(:first-child) svg { color: var(--gray-500); }
.save-policy { width: 100%; min-height: 38px; }
.policy-updated { margin: 11px 0 0; color: var(--gray-500); font-size: 10px; line-height: 1.55; }
.form-state { padding: 20px 0; color: var(--gray-500); font-size: 12px; }
.capability-list { display: flex; flex-direction: column; gap: 0; margin: 14px 0 0; padding: 0; list-style: none; }
.capability-list li { display: grid; grid-template-columns: 20px minmax(0, 1fr) auto; align-items: center; gap: 9px; padding: 11px 0; border-top: 1px solid var(--gray-100); }
.capability-list li > svg { width: 17px; height: 17px; }
.capability-on { color: #208d77; }
.capability-muted { color: var(--gray-400); }
.capability-list span { display: flex; min-width: 0; flex-direction: column; gap: 3px; }
.capability-list strong { color: var(--gray-700); font-size: 11px; }
.capability-list small { color: var(--gray-500); font-size: 9px; line-height: 1.5; }
.capability-list b { color: var(--gray-500); font-size: 9px; font-weight: 600; white-space: nowrap; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; }

@media (max-width: 1150px) {
  .metrics-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .content-grid { grid-template-columns: minmax(0, 1.3fr) minmax(285px, .9fr); }
  .security-hero { align-items: flex-start; }
}
@media (max-width: 920px) {
  .content-grid { grid-template-columns: 1fr; }
  .side-column { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); align-items: start; }
}
@media (max-width: 640px) {
  .security-page { gap: 13px; }
  .security-hero { min-height: unset; flex-direction: column; gap: 17px; padding: 18px; border-radius: 17px; }
  .hero-main { align-items: flex-start; gap: 13px; }
  .shield-art { width: 58px; height: 64px; flex-basis: 58px; border-radius: 18px; }
  .shield-art svg { width: 38px; }
  .shield-spark { top: 7px; right: 8px; font-size: 12px; }
  .hero-copy h2 { font-size: 22px; }
  .hero-copy p { font-size: 12px; line-height: 1.6; }
  .hero-badges { align-items: flex-start; flex-direction: column; gap: 6px; margin-top: 9px; }
  .hero-actions { width: 100%; }
  .hero-actions .button { flex: 1; min-width: 0; padding: 0 9px; font-size: 12px; }
  .scope-note { padding: 10px 11px; gap: 6px 8px; }
  .metrics-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }
  .metric-card { min-height: 100px; gap: 9px; padding: 12px; border-radius: 13px; }
  .metric-icon { width: 31px; height: 31px; flex-basis: 31px; border-radius: 10px; }
  .metric-icon svg { width: 16px; }
  .metric-label { font-size: 10px; line-height: 1.4; }
  .metric-value { font-size: 16px; }
  .metric-value-small { font-size: 11px; }
  .metric-content small { font-size: 9px; line-height: 1.4; }
  .runtime-section, .panel { padding: 14px; border-radius: 14px; }
  .runtime-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .runtime-metric { padding: 11px; border-bottom: 1px solid var(--gray-200); }
  .runtime-metric:nth-child(2) { border-right: 0; }
  .runtime-metric:nth-child(3), .runtime-metric:nth-child(4) { border-bottom: 0; }
  .runtime-metric strong { font-size: 15px; }
  .side-column { grid-template-columns: 1fr; }
  .event-item { grid-template-columns: 3px 24px minmax(0, 1fr); gap: 8px; padding: 13px 0; }
  .event-marker { width: 24px; height: 24px; }
  .event-status { grid-column: 3; justify-self: start; margin-top: 7px; }
  .event-topline { flex-direction: column; gap: 3px; }
  .event-topline time { text-align: left; }
  .event-meta { gap: 5px 9px; }
  .timeline-footer { align-items: flex-start; flex-direction: column; }
  .pager { width: 100%; justify-content: flex-end; }
  .policy-form { gap: 10px; }
}
@media (max-width: 360px) {
  .hero-main { gap: 10px; }
  .hero-copy h2 { font-size: 20px; }
  .metric-card { flex-direction: column; gap: 6px; }
  .metric-content { width: 100%; }
  .policy-form { grid-template-columns: 1fr; }
  .full-field { grid-column: 1; }
}
</style>
