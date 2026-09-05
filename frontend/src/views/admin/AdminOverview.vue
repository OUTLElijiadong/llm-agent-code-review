<template>
  <div class="admin-overview">
    <!-- 顶部:标题 + 安全态势等级 -->
    <header class="ov-head">
      <div>
        <h2 class="ov-title font-display">总览大屏</h2>
        <p class="ov-sub">{{ canViewServer ? '服务器 · 安全态势 · 登录来源 · Agent 活跃 实时一览' : '安全态势 · 登录来源 · Agent 活跃 实时一览' }}</p>
      </div>
      <div class="posture-badge" :class="`lv-${postureLevel}`" role="status">
        <span class="pulse-dot"></span>
        {{ postureLabel }}
      </div>
    </header>

    <!-- 服务器状态 + 安全态势 -->
    <div class="row-2 prism-stagger" :class="{ 'single-column': !canViewServer }">
      <!-- 服务器状态 -->
      <section v-if="canViewServer" class="card prism-glass-card" :data-state="systemState.status" :aria-busy="systemState.status === 'loading'">
        <header class="card-head">
          <h3><el-icon><Monitor /></el-icon>服务器状态</h3>
          <span class="uptime font-mono" v-if="system?.uptime_seconds">运行 {{ formatUptime(system.uptime_seconds) }}</span>
          <button type="button" class="retry-button" :disabled="systemState.status === 'loading'" @click="loadSystem()">{{ loadButtonText(systemState) }}</button>
        </header>
        <p v-if="systemState.status === 'loading'" class="section-state muted" role="status">正在加载服务器状态…<span v-if="systemState.hasResult">显示上次成功数据，等待更新。</span></p>
        <p v-if="systemState.error" class="section-state error" role="alert">服务器状态加载失败：{{ systemState.error }}<span v-if="systemState.hasResult" class="stale-note">上次成功数据已过期</span></p>
        <div v-if="systemState.status === 'empty'" class="muted">{{ system?.available === false ? '服务器已返回：指标采集不可用' : '未取得服务器指标' }}</div>
        <div v-if="system?.available" class="metrics">
          <div class="metric">
            <div class="m-label">CPU</div>
            <div class="m-bar"><i v-if="Number.isFinite(system.cpu_percent)" :style="{ width: system.cpu_percent+'%' }" :class="barClass(system.cpu_percent)"></i></div>
            <div class="m-val font-mono">{{ formatMetric(system.cpu_percent, '%') }}</div>
          </div>
          <div class="metric">
            <div class="m-label">内存</div>
            <div class="m-bar"><i v-if="Number.isFinite(system.memory_percent)" :style="{ width: system.memory_percent+'%' }" :class="barClass(system.memory_percent)"></i></div>
            <div class="m-val font-mono">{{ formatMetric(system.memory_percent, '%') }}</div>
          </div>
          <div class="metric">
            <div class="m-label">磁盘</div>
            <div class="m-bar"><i v-if="Number.isFinite(system.disk_percent)" :style="{ width: system.disk_percent+'%' }" :class="barClass(system.disk_percent)"></i></div>
            <div class="m-val font-mono">{{ formatMetric(system.disk_used_gb) }}/{{ formatMetric(system.disk_total_gb, 'G') }}</div>
          </div>
          <div class="metric" v-if="system.load_avg">
            <div class="m-label">负载</div>
            <div class="m-val font-mono load">{{ system.load_avg.join(' · ') }}</div>
          </div>
        </div>
      </section>

      <!-- 安全态势 -->
      <section class="card prism-glass-card" :data-state="postureState.status" :aria-busy="postureState.status === 'loading'">
        <header class="card-head">
          <h3><el-icon><Aim /></el-icon>安全态势</h3>
          <span class="muted sm">基于应用日志</span>
          <button type="button" class="retry-button" :disabled="postureState.status === 'loading'" @click="loadPosture()">{{ loadButtonText(postureState) }}</button>
        </header>
        <p v-if="postureState.status === 'loading'" class="section-state muted" role="status">正在加载安全态势…<span v-if="postureState.hasResult">显示上次成功数据，等待更新。</span></p>
        <p v-if="postureState.error" class="section-state error" role="alert">安全态势加载失败：{{ postureState.error }}<span v-if="postureState.hasResult" class="stale-note">上次成功数据已过期</span></p>
        <p v-if="postureState.status === 'empty'" class="muted">未取得安全态势，无法判断是否存在异常。</p>
        <div class="posture-grid">
          <div class="p-stat">
            <div class="p-num font-display" :class="{ danger: (posture?.login_failed_24h||0) > 10 }">{{ formatMetric(posture?.login_failed_24h) }}</div>
            <div class="p-label">24h 登录失败</div>
          </div>
          <div class="p-stat">
            <div class="p-num font-display">{{ formatMetric(posture?.login_success_24h) }}</div>
            <div class="p-label">24h 登录成功</div>
          </div>
          <div class="p-stat">
            <div class="p-num font-display" :class="{ danger: (posture?.malware_infected_24h||0) > 0 }">{{ formatMetric(posture?.malware_infected_24h) }}</div>
            <div class="p-label">24h 恶意文件</div>
          </div>
        </div>
        <ul v-if="posture?.signals?.length" class="signals">
          <li
            v-for="(s, i) in posture.signals"
            :key="i"
            :class="`sev-${s.severity}`"
            role="button"
            tabindex="0"
            :title="`去处理:${s.title}`"
            @click="goSignalDetail(s)"
            @keyup.enter="goSignalDetail(s)"
          >
            <span class="sig-icon"><el-icon><WarningFilled /></el-icon></span>
            <div class="sig-main">
              <b>{{ s.title }}</b>
              <p>{{ s.detail }}</p>
            </div>
            <span class="sig-go" aria-hidden="true">去处理 ›</span>
          </li>
        </ul>
        <div v-else-if="postureLevel === 'ok'" class="ok-line">✓ 当前应用日志未发现异常信号</div>
        <div v-else-if="postureState.status === 'success'" class="muted">{{ Array.isArray(posture?.signals) ? '安全态势数据待核验，请刷新后确认。' : '信号数据未取得，无法判断是否存在异常。' }}</div>
      </section>
    </div>

    <!-- 登录来源地图 + Agent 活跃 -->
    <div class="row-2 prism-stagger">
      <!-- 世界地图 -->
      <section class="card map-card prism-glass-card" :data-state="geoState.status" :aria-busy="geoState.status === 'loading'">
        <header class="card-head">
          <h3><el-icon><MapLocation /></el-icon>登录来源分布</h3>
          <span class="muted sm">近30天成功登录 · <template v-if="geoState.hasResult">{{ geoState.status === 'loading' || geoState.error ? '上次结果：' : '' }}{{ geoPoints.length }} 个来源</template><template v-else>来源数未知</template></span>
          <button type="button" class="retry-button" :disabled="geoState.status === 'loading'" @click="loadGeo()">{{ loadButtonText(geoState) }}</button>
        </header>
        <p v-if="geoState.status === 'loading'" class="section-state muted" role="status">正在加载登录来源…<span v-if="geoState.hasResult">显示上次成功数据，等待更新。</span></p>
        <p v-if="geoState.error" class="section-state error" role="alert">登录来源数据加载失败：{{ geoState.error }}<span v-if="geoState.hasResult" class="stale-note">上次成功数据已过期</span></p>
        <div ref="mapRef" class="world-map"></div>
        <div v-if="geoState.status === 'empty'" class="map-state muted">{{ geoState.hasResult ? '暂无可定位的成功登录来源' : '未取得登录来源数据' }}</div>
      </section>

      <!-- Agent 活跃 -->
      <section class="card prism-glass-card" :data-state="agentsState.status" :aria-busy="agentsState.status === 'loading'">
        <header class="card-head">
          <h3><el-icon><Cpu /></el-icon>Agent 活跃状态</h3>
          <div class="live-status" :class="`live-${eventStreamStatus}`">
            <span class="live-dot"></span>{{ liveStatusText }} · <template v-if="agentsState.hasResult && (agentsState.status === 'success' || agentsState.status === 'empty')">{{ workingCount }} 个运行中</template><template v-else>运行状态未知</template>
          </div>
          <button type="button" class="retry-button" :disabled="agentsState.status === 'loading'" @click="loadAgents()">{{ loadButtonText(agentsState) }}</button>
        </header>
        <p v-if="agentsState.status === 'loading'" class="section-state muted" role="status">正在加载 Agent 活跃状态…<span v-if="agentsState.hasResult">显示上次成功数据，等待更新。</span></p>
        <p v-if="agentsState.error" class="section-state error" role="alert">Agent 数据加载失败：{{ agentsState.error }}<span v-if="agentsState.hasResult" class="stale-note">上次成功数据已过期</span></p>
        <ul class="agent-list">
          <li v-for="a in agents" :key="a.agent_code" class="agent-item" :class="a.status">
            <span class="a-avatar" :class="a.status">
              <span v-if="['working', 'thinking', 'blocked'].includes(a.status)" class="ring"></span>
              <span v-if="['working', 'thinking'].includes(a.status)" class="activity-bars" aria-hidden="true"><i></i><i></i><i></i></span>
              {{ agentEmoji(a) }}
            </span>
            <div class="a-info">
              <div class="a-name">{{ a.name }}</div>
              <div class="a-purpose">{{ a.purpose || '待命' }}</div>
            </div>
            <div class="a-meta">
              <span class="a-status" :class="a.status">{{ statusText(a.status) }}</span>
              <button
                type="button"
                class="a-calls font-mono"
                :title="formatAgentActivityUsage(a).title"
                :aria-label="formatAgentActivityUsage(a).title"
                :aria-expanded="expandedUsageCode === a.agent_code"
                @click="toggleAgentUsage(a.agent_code)"
              >
                {{ formatAgentActivityUsage(a).label }}
              </button>
              <span v-if="expandedUsageCode === a.agent_code" class="a-usage-detail">
                {{ formatAgentActivityUsage(a).title }}
              </span>
            </div>
          </li>
          <li v-if="agentsState.status === 'empty'" class="muted center">{{ agentsState.hasResult ? '暂无 Agent 数据' : '未取得 Agent 数据' }}</li>
        </ul>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Aim, Cpu, MapLocation, Monitor, WarningFilled } from '@element-plus/icons-vue'
import * as echarts from 'echarts/core'
import { GeoComponent, TooltipComponent, VisualMapComponent } from 'echarts/components'
import { EffectScatterChart, ScatterChart } from 'echarts/charts'
import { CanvasRenderer } from 'echarts/renderers'
import worldJson from '@/assets/maps/world.json'
import {
  getAgentsActivity,
  getLoginGeo,
  getSecurityPosture,
  getSystemStatus,
  type AgentActivity,
  type GeoPoint,
  type SecurityPosture,
  type SystemStatus,
} from '@/api/adminOverview'
import { subscribeAgentEvents } from '@/utils/agentEventStream'
import type { AgentEvent } from '@/types/agentEvent'
import { useUserStore } from '@/stores/user'
import { formatAgentActivityUsage } from '@/utils/agentActivityPresentation'

echarts.use([GeoComponent, TooltipComponent, VisualMapComponent, ScatterChart, EffectScatterChart, CanvasRenderer])

const router = useRouter()

/** 告警即入口:按信号语义跳到对应处置页(开放告警/审计检索)。 */
function goSignalDetail(signal: { title?: string; detail?: string; severity?: string }): void {
  const text = `${signal.title ?? ''}${signal.detail ?? ''}`
  if (/登录|爆破|暴力/.test(text)) {
    void router.push({ path: '/admin/audit', query: { keyword: '登录' } })
    return
  }
  if (/文件|恶意|样本/.test(text)) {
    void router.push('/admin/observability')
    return
  }
  void router.push('/admin/observability')
}

const system = ref<SystemStatus | null>(null)
const userStore = useUserStore()
const canViewServer = computed(() => {
  const profile = userStore.profile
  return profile?.username === 'admin' && profile.status === 1 &&
    profile.role === 'super_admin' && userStore.roles.includes('super_admin')
})
const posture = ref<SecurityPosture | null>(null)
const geoPoints = ref<GeoPoint[]>([])
const agents = ref<AgentActivity[]>([])
interface LoadState {
  status: 'loading' | 'error' | 'empty' | 'success'
  error: string
  hasResult: boolean
  permissionDenied: boolean
}
function createLoadState(): LoadState {
  return reactive({ status: 'loading', error: '', hasResult: false, permissionDenied: false })
}
const systemState = createLoadState()
const postureState = createLoadState()
const geoState = createLoadState()
const agentsState = createLoadState()
const pendingRequests = new Set<LoadState>()
const expandedUsageCode = ref<string | null>(null)
const mapRef = ref<HTMLElement | null>(null)
let mapChart: echarts.EChartsType | null = null
let timer: ReturnType<typeof setInterval> | null = null
let eventStream: { close: () => void } | null = null
const eventStreamStatus = ref<'connecting' | 'connected' | 'reconnecting' | 'closed'>('connecting')
let disposed = false

const postureLevel = computed(() => {
  if (postureState.status !== 'success' || !posture.value) return 'unknown'
  const value = posture.value
  if (value.level === 'attack' || value.level === 'suspicious') return value.level
  if (value.level === 'ok' && Array.isArray(value.signals) && value.signals.length === 0 &&
    [value.login_failed_24h, value.login_success_24h, value.malware_infected_24h].every((count) => Number.isFinite(count))) return 'ok'
  return 'unknown'
})
const postureLabel = computed(() => {
  if (postureState.status === 'loading') return '安全态势：加载中'
  if (postureLevel.value === 'attack') return '安全态势：检测到攻击迹象'
  if (postureLevel.value === 'suspicious') return '安全态势：存在可疑活动'
  if (postureLevel.value === 'ok') return '安全态势：日志未见异常'
  return '安全态势：未知'
})
const workingCount = computed(() => agents.value.filter((a) => ['working', 'thinking', 'blocked'].includes(a.status)).length)
const liveStatusText = computed(() => ({
  connecting: '正在连接', connected: '实时连接', reconnecting: '重连中', closed: '已断开',
} as Record<string, string>)[eventStreamStatus.value])

function statusText(s: string): string {
  return { idle: '待命', thinking: '思考中', working: '执行中', blocked: '等待输入', error: '异常', disabled: '已停用' }[s] || s
}
function agentEmoji(a: AgentActivity): string {
  if (a.status === 'thinking') return '◌'
  if (a.status === 'working') return '⚙️'
  if (a.status === 'blocked') return '⏸️'
  if (a.status === 'error') return '🔴'
  if (a.is_enabled === 0) return '⏸️'
  return '🤖'
}
function toggleAgentUsage(agentCode: string): void {
  expandedUsageCode.value = expandedUsageCode.value === agentCode ? null : agentCode
}
function barClass(v?: number): string {
  const n = v || 0
  if (n >= 85) return 'danger'
  if (n >= 65) return 'warn'
  return 'ok'
}
function formatMetric(value: unknown, suffix = ''): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${value}${suffix}` : '未知'
}
function formatUptime(sec: number): string {
  const d = Math.floor(sec / 86400)
  const h = Math.floor((sec % 86400) / 3600)
  return d > 0 ? `${d}天${h}时` : `${h}时${Math.floor((sec % 3600) / 60)}分`
}

function renderMap(): void {
  if (disposed || !mapRef.value) return
  if (!mapChart) {
    echarts.registerMap('world', worldJson as never)
    mapChart = echarts.init(mapRef.value)
  }
  const data = geoPoints.value.map((p) => ({
    name: `${p.city || p.country || p.ip}(${p.count})`,
    value: [p.longitude, p.latitude, p.count],
    ip: p.ip,
    label: `${p.country || ''} ${p.city || ''}`.trim() || p.ip,
  }))
  const max = Math.max(0, ...geoPoints.value.map((p) => p.count))
  mapChart.setOption({
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      appendTo: 'body',
      formatter: (p: { data?: { label?: string; value?: number[]; ip?: string } }) => {
        const d = p.data
        if (!d?.value) return ''
        return `<b>${d.label || ''}</b><br/>IP:${d.ip || ''}<br/>登录 ${formatMetric(d.value[2])} 次`
      },
    },
    geo: {
      map: 'world',
      roam: true,
      zoom: 1.15,
      label: { show: false },
      itemStyle: { areaColor: '#EEF1F7', borderColor: '#C9D2E3', borderWidth: 0.5 },
      emphasis: { itemStyle: { areaColor: '#C7CBE8' }, label: { show: false } },
    },
    visualMap: data.length ? {
      show: true,
      min: 0, max, left: 8, bottom: 8,
      text: ['多', '少'], calculable: true,
      inRange: { color: ['#8EA2F8', '#5B58E8', '#C92A6E'] },
      textStyle: { color: '#6B7280', fontSize: 11 },
    } : { show: false },
    series: [
      {
        type: 'effectScatter',
        coordinateSystem: 'geo',
        data,
        symbolSize: (v: number[]) => Math.max(5, Math.sqrt(v[2]) * 2.2),
        rippleEffect: { brushType: 'stroke', scale: 3 },
        itemStyle: { shadowBlur: 6, shadowColor: 'rgba(91,88,232,0.4)' },
        zlevel: 2,
      },
    ],
  }, true)
}

function loadButtonText(state: LoadState): string {
  if (state.status === 'loading') return '加载中…'
  return state.status === 'error' ? '重试' : '刷新'
}

function loadErrorDetails(error: unknown): { message: string; permissionDenied: boolean } {
  const failure = error as {
    message?: string
    code?: number
    next_action?: string
    response?: { status?: number; data?: { message?: string; code?: number; next_action?: string } }
  } | null
  const detail = failure?.response?.data ?? failure
  const forbidden = failure?.response?.status === 403 || detail?.code === 403 || Math.floor((detail?.code ?? 0) / 100) === 403
  let message = detail?.message || (forbidden ? '读取权限不足（403），请确认当前账号权限' : '请求失败，请稍后重试')
  if (detail?.next_action) message += `；${detail.next_action}`
  if (forbidden) message += '；自动刷新已暂停，请确认权限后手动重试。'
  return { message, permissionDenied: forbidden }
}

async function loadSection<T>(state: LoadState, request: () => Promise<T>, accept: (value: T) => boolean, manual: boolean): Promise<void> {
  if (disposed || pendingRequests.has(state) || (!manual && state.permissionDenied)) return
  pendingRequests.add(state)
  state.status = 'loading'
  try {
    const result = await request()
    if (disposed) return
    state.status = accept(result) ? 'success' : 'empty'
    state.hasResult = result != null
    state.error = ''
    state.permissionDenied = false
  } catch (error) {
    if (!disposed) {
      state.status = 'error'
      const failure = loadErrorDetails(error)
      state.error = failure.message
      state.permissionDenied = failure.permissionDenied
    }
  } finally {
    pendingRequests.delete(state)
  }
}

async function loadSystem(manual = true): Promise<void> {
  if (!canViewServer.value) return
  await loadSection(systemState, getSystemStatus, (value) => {
    system.value = value ?? null
    return value?.available === true
  }, manual)
}

async function loadPosture(manual = true): Promise<void> {
  await loadSection(postureState, getSecurityPosture, (value) => {
    posture.value = value ?? null
    return value != null
  }, manual)
}

async function loadGeo(manual = true): Promise<void> {
  await loadSection(geoState, () => {
    renderMap()
    return getLoginGeo()
  }, (value) => {
    geoPoints.value = value ?? []
    renderMap()
    return geoPoints.value.length > 0
  }, manual)
}

async function loadAgents(manual = true): Promise<void> {
  await loadSection(agentsState, getAgentsActivity, (value) => {
    agents.value = value ?? []
    return agents.value.length > 0
  }, manual)
}

async function loadAll(): Promise<void> {
  await Promise.all([loadSystem(false), loadPosture(false), loadGeo(false), loadAgents(false)])
}

function applyAgentEvent(event: AgentEvent): void {
  const statusMap: Record<string, AgentActivity['status']> = {
    dispatch: 'thinking', thinking: 'thinking', progress: 'working',
    complete: 'idle', failed: 'error', clarify: 'blocked',
  }
  const status = statusMap[event.type]
  if (!status) return
  const agent = agents.value.find((item) => item.agent_code === event.agent)
  if (!agent) return
  agent.status = status
  agent.purpose = event.message || agent.purpose
  agent.last_seen_at = event.timestamp
  agent.activity_source = 'event_bus'
}

function onResize(): void { mapChart?.resize() }

onMounted(() => {
  loadAll()
  eventStream = subscribeAgentEvents(applyAgentEvent, {
    replay: 10,
    onStatus: (status) => { eventStreamStatus.value = status },
  })
  window.addEventListener('resize', onResize)
  timer = setInterval(loadAll, 5_000) // SSE 实时事件 + 5s 数据兜底
})
onBeforeUnmount(() => {
  disposed = true
  window.removeEventListener('resize', onResize)
  if (timer) clearInterval(timer)
  eventStream?.close()
  mapChart?.dispose()
  mapChart = null
})
</script>

<style scoped lang="scss">
.admin-overview { display: flex; flex-direction: column; gap: 16px; }

.ov-head { display: flex; flex-wrap: wrap; gap: 12px; justify-content: space-between; align-items: flex-end; }
.ov-title { margin: 0; font-size: 22px; font-weight: 600; color: var(--gray-900); }
.ov-sub { margin: 4px 0 0; font-size: 12.5px; color: var(--gray-500); }

.posture-badge {
  display: flex; align-items: center; gap: 8px;
  flex-shrink: 0; white-space: nowrap;
  padding: 8px 16px; border-radius: 999px;
  font-size: 13px; font-weight: 600;
  .pulse-dot { width: 9px; height: 9px; border-radius: 50%; animation: pulse 1.6s infinite; }
  &.lv-ok { background: rgba(79,184,122,.12); color: #2F8F5B; .pulse-dot { background: #4FB87A; } }
  &.lv-suspicious { background: rgba(217,168,87,.14); color: #B9832F; .pulse-dot { background: #D9A857; } }
  &.lv-attack { background: rgba(220,73,97,.12); color: #C92A4E; .pulse-dot { background: #DC4961; } }
  &.lv-unknown { background: var(--gray-100); color: var(--gray-600); .pulse-dot { background: currentColor; animation: none; } }
}
@keyframes pulse { 0%,100% { opacity: 1; transform: scale(1);} 50% { opacity: .4; transform: scale(.8);} }

.row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.row-2.single-column { grid-template-columns: 1fr; }
@media (max-width: 1100px) { .row-2 { grid-template-columns: 1fr; } }

.card {
  position: relative;
  overflow: hidden;
  background: var(--surface-glass);
  border: 1px solid rgba(224, 227, 234, 0.9);
  border-radius: 10px;
  padding: 18px 20px;
  box-shadow: var(--panel-shadow);
  backdrop-filter: blur(14px) saturate(1.08);
  -webkit-backdrop-filter: blur(14px) saturate(1.08);
}
.card::before {
  content: '';
  position: absolute;
  inset: 0 0 auto;
  height: 2px;
  background: linear-gradient(90deg, var(--brand-300), var(--accent-400), transparent 82%);
  opacity: 0.72;
  pointer-events: none;
}
.card-head { display: flex; flex-wrap: wrap; gap: 8px; justify-content: space-between; align-items: center; margin-bottom: 14px;
  h3 { margin: 0; font-size: 15px; font-weight: 600; color: var(--gray-900); display: flex; align-items: center; gap: 7px; }
}
.live-status { display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; color: var(--gray-500); white-space: nowrap;
  &.live-connected { color: #2F8F5B; }
  &.live-reconnecting, &.live-connecting { color: #B9832F; }
  &.live-closed { color: #C92A4E; }
}
.live-dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; animation: pulse 1.2s infinite; }
.uptime { font-size: 11.5px; color: var(--gray-500); }
.muted { color: var(--gray-400); font-size: 12.5px; &.sm { font-size: 11.5px; } &.center { text-align: center; padding: 24px 0; } }
.section-state { margin: 0 0 12px; font-size: 12.5px; line-height: 1.6; overflow-wrap: anywhere; }
.section-state.error { color: #C92A4E; }
.stale-note { display: block; font-weight: 600; }
.retry-button {
  flex-shrink: 0; padding: 4px 10px; border: 1px solid var(--gray-300); border-radius: 6px;
  background: var(--gray-50); color: var(--brand-600); font: inherit; font-size: 12px; cursor: pointer;
  &:disabled { cursor: wait; color: var(--gray-500); }
  &:focus-visible { outline: 2px solid var(--brand-300); outline-offset: 2px; }
}

.metrics { display: flex; flex-direction: column; gap: 13px; }
.metric { display: flex; align-items: center; gap: 12px; }
.m-label { width: 38px; font-size: 12.5px; color: var(--gray-600); }
.m-bar { flex: 1; height: 8px; background: var(--gray-100, #F0F2F7); border-radius: 999px; overflow: hidden;
  i { display: block; height: 100%; border-radius: 999px; transition: width .6s;
    &.ok { background: linear-gradient(90deg,#5B58E8,#4FB87A); }
    &.warn { background: #D9A857; }
    &.danger { background: #DC4961; }
  }
}
.m-val { width: 92px; text-align: right; font-size: 12px; color: var(--gray-700); &.load { width: auto; } }

.posture-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 12px; }
.p-stat { text-align: center; padding: 10px; background: var(--gray-50, #F8F9FC); border-radius: 10px; }
.p-num { font-size: 24px; font-weight: 700; color: var(--gray-900); &.danger { color: #DC4961; } }
.p-label { font-size: 11px; color: var(--gray-500); margin-top: 2px; }

.signals li { cursor: pointer; transition: background 0.15s ease, transform 0.15s ease; }
.signals li:hover, .signals li:focus-visible { background: var(--gray-50); transform: translateX(2px); outline: none; }
.signals li:focus-visible { box-shadow: inset 0 0 0 2px var(--brand-300); }
.sig-main { min-width: 0; flex: 1; }
.sig-go { flex: none; align-self: center; color: var(--brand-600); font-size: 11px; opacity: 0; transition: opacity 0.15s ease; }
.signals li:hover .sig-go, .signals li:focus-visible .sig-go { opacity: 1; }
.signals { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; max-height: 150px; overflow: auto;
  li { display: flex; gap: 9px; padding: 8px 10px; border-radius: 8px; font-size: 12px;
    &.sev-high { background: rgba(220,73,97,.07); b { color: #C92A4E; } .sig-icon { color: #DC4961; } }
    &.sev-medium { background: rgba(217,168,87,.09); b { color: #B9832F; } .sig-icon { color: #D9A857; } }
    b { font-size: 12.5px; } p { margin: 2px 0 0; color: var(--gray-600); }
  }
}
.ok-line { color: #2F8F5B; font-size: 13px; padding: 8px 0; }

.map-card { display: flex; flex-direction: column; }
.world-map { width: 100%; height: 320px; }
.map-state { margin-top: -24px; padding-bottom: 8px; text-align: center; font-size: 12.5px; }
.map-state.error { color: #C92A4E; }

.agent-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; max-height: 320px; overflow: auto; }
.agent-item { display: flex; align-items: center; gap: 12px; padding: 9px 10px; border-radius: 10px; transition: background .15s;
  &:hover { background: var(--gray-50, #F8F9FC); }
}
.a-avatar { position: relative; width: 38px; height: 38px; border-radius: 10px; display: flex; align-items: center; justify-content: center;
  font-size: 18px; background: var(--gray-100, #EFF1F6); flex-shrink: 0;
  &.working, &.thinking { background: rgba(91,88,232,.12); }
  &.blocked { background: rgba(217,168,87,.14); }
  &.error { background: rgba(220,73,97,.12); }
  .ring { position: absolute; inset: -3px; border-radius: 12px; border: 2px solid #5B58E8; border-top-color: transparent; animation: spin 1.1s linear infinite; }
}
.agent-item.working, .agent-item.thinking { background: linear-gradient(90deg, rgba(91,88,232,.06), transparent 72%); }
.agent-item.blocked { background: linear-gradient(90deg, rgba(217,168,87,.08), transparent 72%); }
.activity-bars { position: absolute; right: 4px; bottom: 4px; display: flex; align-items: flex-end; gap: 2px; height: 9px;
  i { display: block; width: 2px; height: 4px; border-radius: 2px; background: #5B58E8; animation: activity-bar 1s ease-in-out infinite alternate; }
  i:nth-child(2) { height: 8px; animation-delay: .18s; }
  i:nth-child(3) { height: 6px; animation-delay: .36s; }
}
@keyframes activity-bar { from { transform: scaleY(.45); opacity: .45; } to { transform: scaleY(1); opacity: 1; } }
@keyframes spin { to { transform: rotate(360deg); } }
.a-info { flex: 1; min-width: 0; }
.a-name { font-size: 13.5px; font-weight: 600; color: var(--gray-900); }
.a-purpose { font-size: 11.5px; color: var(--gray-500); margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.a-meta {
  display: flex;
  flex: 0 1 min(44%, 220px);
  min-width: 0;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
}
.a-status { font-size: 11px; padding: 1px 8px; border-radius: 999px; font-weight: 600;
  &.working, &.thinking { background: rgba(91,88,232,.12); color: #4B48D8; }
  &.blocked { background: rgba(217,168,87,.14); color: #B9832F; }
  &.idle { background: var(--gray-100, #EFF1F6); color: var(--gray-500); }
  &.error { background: rgba(220,73,97,.12); color: #C92A4E; }
  &.disabled { background: var(--gray-100); color: var(--gray-400); }
}
.a-calls {
  appearance: none;
  border: 0;
  padding: 0;
  background: transparent;
  max-width: 100%;
  min-width: 0;
  color: var(--gray-500);
  font-size: 10.5px;
  line-height: 1.35;
  text-align: right;
  overflow-wrap: anywhere;
  cursor: help;
}
.a-calls:hover,
.a-calls:focus-visible { color: var(--brand-600); }
.a-calls:focus-visible { outline: 2px solid var(--brand-300); outline-offset: 2px; border-radius: 3px; }
.a-usage-detail {
  max-width: 100%;
  color: var(--gray-600);
  font-size: 10.5px;
  line-height: 1.4;
  text-align: right;
  overflow-wrap: anywhere;
}

@media (max-width: 560px) {
  .card { padding: 16px 14px; }
  .card-head { align-items: flex-start; gap: 8px; }
  .card-head h3 { min-width: 0; }
  .live-status { white-space: normal; text-align: right; }
  .agent-item { align-items: flex-start; flex-wrap: wrap; gap: 8px 10px; }
  .a-info { flex: 1 1 calc(100% - 50px); }
  .a-meta {
    flex: 1 1 100%;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: center;
    gap: 6px;
    margin-left: 48px;
  }
  .a-calls { text-align: left; }
  .a-usage-detail { grid-column: 1 / -1; text-align: left; }
}

</style>
