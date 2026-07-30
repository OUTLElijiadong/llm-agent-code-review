<template>
  <div class="admin-overview">
    <!-- 顶部:标题 + 安全态势等级 -->
    <header class="ov-head">
      <div>
        <h2 class="ov-title font-display">总览大屏</h2>
        <p class="ov-sub">服务器 · 安全态势 · 登录来源 · Agent 活跃 实时一览</p>
      </div>
      <div class="posture-badge" :class="`lv-${posture?.level || 'ok'}`">
        <span class="pulse-dot"></span>
        {{ postureLabel }}
      </div>
    </header>

    <!-- 服务器状态 + 安全态势 -->
    <div class="row-2">
      <!-- 服务器状态 -->
      <section class="card">
        <header class="card-head">
          <h3><el-icon><Monitor /></el-icon>服务器状态</h3>
          <span class="uptime font-mono" v-if="system?.uptime_seconds">运行 {{ formatUptime(system.uptime_seconds) }}</span>
        </header>
        <div v-if="!system?.available" class="muted">系统指标不可用(psutil 未就绪)</div>
        <div v-else class="metrics">
          <div class="metric">
            <div class="m-label">CPU</div>
            <div class="m-bar"><i :style="{ width: (system.cpu_percent||0)+'%' }" :class="barClass(system.cpu_percent)"></i></div>
            <div class="m-val font-mono">{{ system.cpu_percent }}%</div>
          </div>
          <div class="metric">
            <div class="m-label">内存</div>
            <div class="m-bar"><i :style="{ width: (system.memory_percent||0)+'%' }" :class="barClass(system.memory_percent)"></i></div>
            <div class="m-val font-mono">{{ system.memory_percent }}%</div>
          </div>
          <div class="metric">
            <div class="m-label">磁盘</div>
            <div class="m-bar"><i :style="{ width: (system.disk_percent||0)+'%' }" :class="barClass(system.disk_percent)"></i></div>
            <div class="m-val font-mono">{{ system.disk_used_gb }}/{{ system.disk_total_gb }}G</div>
          </div>
          <div class="metric" v-if="system.load_avg">
            <div class="m-label">负载</div>
            <div class="m-val font-mono load">{{ system.load_avg.join(' · ') }}</div>
          </div>
        </div>
      </section>

      <!-- 安全态势 -->
      <section class="card">
        <header class="card-head">
          <h3><el-icon><Aim /></el-icon>安全态势</h3>
          <span class="muted sm">基于应用日志</span>
        </header>
        <div class="posture-grid">
          <div class="p-stat">
            <div class="p-num font-display" :class="{ danger: (posture?.login_failed_24h||0) > 10 }">{{ posture?.login_failed_24h ?? '-' }}</div>
            <div class="p-label">24h 登录失败</div>
          </div>
          <div class="p-stat">
            <div class="p-num font-display">{{ posture?.login_success_24h ?? '-' }}</div>
            <div class="p-label">24h 登录成功</div>
          </div>
          <div class="p-stat">
            <div class="p-num font-display" :class="{ danger: (posture?.malware_infected_24h||0) > 0 }">{{ posture?.malware_infected_24h ?? 0 }}</div>
            <div class="p-label">24h 恶意文件</div>
          </div>
        </div>
        <ul v-if="posture?.signals?.length" class="signals">
          <li v-for="(s, i) in posture.signals" :key="i" :class="`sev-${s.severity}`">
            <span class="sig-icon">⚠</span>
            <div>
              <b>{{ s.title }}</b>
              <p>{{ s.detail }}</p>
            </div>
          </li>
        </ul>
        <div v-else class="ok-line">✓ 未发现爆破/恶意扫描迹象</div>
      </section>
    </div>

    <!-- 登录来源地图 + Agent 活跃 -->
    <div class="row-2">
      <!-- 世界地图 -->
      <section class="card map-card">
        <header class="card-head">
          <h3><el-icon><MapLocation /></el-icon>登录来源分布</h3>
          <span class="muted sm">近30天 · {{ geoPoints.length }} 个来源</span>
        </header>
        <div ref="mapRef" class="world-map"></div>
        <div v-if="!geoPoints.length" class="muted center">暂无可定位的登录来源</div>
      </section>

      <!-- Agent 活跃 -->
      <section class="card">
        <header class="card-head">
          <h3><el-icon><Cpu /></el-icon>Agent 活跃状态</h3>
          <span class="muted sm">{{ workingCount }} 个运行中</span>
        </header>
        <ul class="agent-list">
          <li v-for="a in agents" :key="a.agent_code" class="agent-item">
            <span class="a-avatar" :class="a.status">
              <span v-if="a.status === 'working'" class="ring"></span>
              {{ agentEmoji(a) }}
            </span>
            <div class="a-info">
              <div class="a-name">{{ a.name }}</div>
              <div class="a-purpose">{{ a.purpose || '待命' }}</div>
            </div>
            <div class="a-meta">
              <span class="a-status" :class="a.status">{{ statusText(a.status) }}</span>
              <span class="a-calls font-mono">{{ a.calls_today }} 次/今日</span>
            </div>
          </li>
          <li v-if="!agents.length" class="muted center">暂无 Agent 数据</li>
        </ul>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Aim, Cpu, MapLocation, Monitor } from '@element-plus/icons-vue'
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

echarts.use([GeoComponent, TooltipComponent, VisualMapComponent, ScatterChart, EffectScatterChart, CanvasRenderer])

const system = ref<SystemStatus | null>(null)
const posture = ref<SecurityPosture | null>(null)
const geoPoints = ref<GeoPoint[]>([])
const agents = ref<AgentActivity[]>([])
const mapRef = ref<HTMLElement | null>(null)
let mapChart: echarts.EChartsType | null = null
let timer: ReturnType<typeof setInterval> | null = null

const postureLabel = computed(() => {
  const lv = posture.value?.level
  if (lv === 'attack') return '检测到攻击迹象'
  if (lv === 'suspicious') return '存在可疑活动'
  return '系统正常'
})
const workingCount = computed(() => agents.value.filter((a) => a.status === 'working').length)

function statusText(s: string): string {
  return { idle: '待命', working: '运行中', error: '异常', disabled: '已停用' }[s] || s
}
function agentEmoji(a: AgentActivity): string {
  if (a.status === 'working') return '⚙️'
  if (a.status === 'error') return '🔴'
  if (a.is_enabled === 0) return '⏸️'
  return '🤖'
}
function barClass(v?: number): string {
  const n = v || 0
  if (n >= 85) return 'danger'
  if (n >= 65) return 'warn'
  return 'ok'
}
function formatUptime(sec: number): string {
  const d = Math.floor(sec / 86400)
  const h = Math.floor((sec % 86400) / 3600)
  return d > 0 ? `${d}天${h}时` : `${h}时${Math.floor((sec % 3600) / 60)}分`
}

function renderMap(): void {
  if (!mapRef.value) return
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
  const max = Math.max(1, ...geoPoints.value.map((p) => p.count))
  mapChart.setOption({
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      formatter: (p: { data?: { label?: string; value?: number[]; ip?: string } }) => {
        const d = p.data || {}
        return `<b>${d.label || ''}</b><br/>IP:${d.ip || ''}<br/>登录 ${d.value?.[2] ?? 0} 次`
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
    visualMap: {
      min: 0, max, left: 8, bottom: 8,
      text: ['多', '少'], calculable: true,
      inRange: { color: ['#8EA2F8', '#5B58E8', '#C92A6E'] },
      textStyle: { color: '#6B7280', fontSize: 11 },
    },
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

async function loadAll(): Promise<void> {
  const [sys, sec, geo, ag] = await Promise.allSettled([
    getSystemStatus(), getSecurityPosture(), getLoginGeo(), getAgentsActivity(),
  ])
  if (sys.status === 'fulfilled') system.value = sys.value
  if (sec.status === 'fulfilled') posture.value = sec.value
  if (geo.status === 'fulfilled') geoPoints.value = geo.value
  if (ag.status === 'fulfilled') agents.value = ag.value
  renderMap()
}

function onResize(): void { mapChart?.resize() }

onMounted(() => {
  loadAll()
  window.addEventListener('resize', onResize)
  timer = setInterval(loadAll, 30_000) // 30s 轻量刷新态势
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  if (timer) clearInterval(timer)
  mapChart?.dispose()
})
</script>

<style scoped lang="scss">
.admin-overview { display: flex; flex-direction: column; gap: 16px; }

.ov-head { display: flex; justify-content: space-between; align-items: flex-end; }
.ov-title { margin: 0; font-size: 22px; font-weight: 600; color: var(--gray-900); }
.ov-sub { margin: 4px 0 0; font-size: 12.5px; color: var(--gray-500); }

.posture-badge {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 16px; border-radius: 999px;
  font-size: 13px; font-weight: 600;
  .pulse-dot { width: 9px; height: 9px; border-radius: 50%; animation: pulse 1.6s infinite; }
  &.lv-ok { background: rgba(79,184,122,.12); color: #2F8F5B; .pulse-dot { background: #4FB87A; } }
  &.lv-suspicious { background: rgba(217,168,87,.14); color: #B9832F; .pulse-dot { background: #D9A857; } }
  &.lv-attack { background: rgba(220,73,97,.12); color: #C92A4E; .pulse-dot { background: #DC4961; } }
}
@keyframes pulse { 0%,100% { opacity: 1; transform: scale(1);} 50% { opacity: .4; transform: scale(.8);} }

.row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 1100px) { .row-2 { grid-template-columns: 1fr; } }

.card {
  background: #fff; border: 1px solid var(--gray-200, #E5E8F0);
  border-radius: 14px; padding: 18px 20px;
  box-shadow: 0 2px 8px rgba(31,36,82,.04);
}
.card-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;
  h3 { margin: 0; font-size: 15px; font-weight: 600; color: var(--gray-900); display: flex; align-items: center; gap: 7px; }
}
.uptime { font-size: 11.5px; color: var(--gray-500); }
.muted { color: var(--gray-400); font-size: 12.5px; &.sm { font-size: 11.5px; } &.center { text-align: center; padding: 24px 0; } }

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

.agent-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; max-height: 320px; overflow: auto; }
.agent-item { display: flex; align-items: center; gap: 12px; padding: 9px 10px; border-radius: 10px; transition: background .15s;
  &:hover { background: var(--gray-50, #F8F9FC); }
}
.a-avatar { position: relative; width: 38px; height: 38px; border-radius: 10px; display: flex; align-items: center; justify-content: center;
  font-size: 18px; background: var(--gray-100, #EFF1F6); flex-shrink: 0;
  &.working { background: rgba(91,88,232,.12); }
  &.error { background: rgba(220,73,97,.12); }
  .ring { position: absolute; inset: -3px; border-radius: 12px; border: 2px solid #5B58E8; border-top-color: transparent; animation: spin 1.1s linear infinite; }
}
@keyframes spin { to { transform: rotate(360deg); } }
.a-info { flex: 1; min-width: 0; }
.a-name { font-size: 13.5px; font-weight: 600; color: var(--gray-900); }
.a-purpose { font-size: 11.5px; color: var(--gray-500); margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.a-meta { display: flex; flex-direction: column; align-items: flex-end; gap: 2px; }
.a-status { font-size: 11px; padding: 1px 8px; border-radius: 999px; font-weight: 600;
  &.working { background: rgba(91,88,232,.12); color: #4B48D8; }
  &.idle { background: var(--gray-100, #EFF1F6); color: var(--gray-500); }
  &.error { background: rgba(220,73,97,.12); color: #C92A4E; }
  &.disabled { background: var(--gray-100); color: var(--gray-400); }
}
.a-calls { font-size: 10.5px; color: var(--gray-400); }
</style>
