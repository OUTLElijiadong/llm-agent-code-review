<template>
  <main class="usage-page">
    <header class="page-head">
      <div>
        <h1>我的 Token 用量</h1>
      </div>
      <div class="controls">
        <label for="usage-days">统计时间</label>
        <select id="usage-days" v-model.number="days" @change="loadUsage">
          <option :value="7">最近 7 天</option>
          <option :value="30">最近 30 天</option>
          <option :value="90">最近 90 天</option>
        </select>
        <button type="button" :disabled="loading" @click="loadUsage">刷新</button>
      </div>
    </header>
    <nav class="usage-views" aria-label="用量视图">
      <button type="button" data-view="logs" :aria-pressed="view === 'logs'" aria-controls="usage-log-view" @click="selectView('logs')">调用日志</button>
      <button type="button" data-view="billing" :aria-pressed="view === 'billing'" aria-controls="usage-billing-view" @click="selectView('billing')">计费</button>
    </nav>
    <p v-if="billingError && view !== 'billing'" class="notice error" role="alert">计费账本加载失败，请切换到计费视图重试。</p>
    <p v-if="error && view !== 'logs'" class="notice error" role="alert">调用日志加载失败，请切换到调用日志视图重试。</p>
    <p v-if="loading && !usage && !billingUsage" class="notice" role="status">正在加载用量…</p>

    <div id="usage-log-view" v-show="view === 'logs'">
    <div v-if="error && !usage" class="notice error" role="alert">
      <p>旧模型调用日志加载失败；计费账本会单独显示，请检查网络后重试。</p>
      <button type="button" @click="loadUsage">重新加载</button>
    </div>
    <template v-if="usage">
      <p v-if="error" class="notice error" role="alert">刷新失败，当前显示上次成功加载的数据。</p>
      <div class="summary token-summary" :aria-busy="loading">
        <section><span>已记录 Token</span><strong>{{ number(usage.total_tokens) }}</strong></section>
        <section><span>已记录输入 Token</span><strong>{{ number(usage.input_tokens) }}</strong></section>
        <section><span>已记录输出 Token</span><strong>{{ number(usage.output_tokens) }}</strong></section>
        <section><span>已知用量调用</span><strong>{{ number(usage.known_calls) }}</strong></section>
        <section><span>用量未知调用</span><strong>{{ number(usage.unknown_calls) }}</strong></section>
      </div>
      <section class="card" :aria-busy="loading">
        <div class="card-head"><h2>每日趋势</h2><span>{{ usage.start_date }} 至 {{ usage.end_date }} · UTC</span></div>
        <div ref="chartElement" class="chart" role="img" aria-label="每日 Token 用量趋势；详细数据可在下方表格查看" />
        <p v-if="usage.unknown_calls" class="note">部分调用缺少完整用量，统计仅包含已知记录；仅有未知用量的日期在图中显示为断点。</p>
        <p v-else-if="usage.known_calls === 0" class="note">这段时间没有模型调用记录。</p>
        <p v-if="usage.component_unknown_calls" class="note">{{ number(usage.component_unknown_calls) }} 次调用缺少输入或输出分项，分项合计不代表完整消耗。</p>
      </section>
      <section class="card">
        <h2>每日明细</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>日期（UTC）</th><th>Token</th><th>调用次数</th><th>未知用量</th></tr></thead>
            <tbody><tr v-for="day in [...usage.daily].reverse()" :key="day.date">
              <td>{{ day.date }}</td><td>{{ day.tokens === null ? '未知' : number(day.tokens) }}</td>
              <td>{{ number(day.calls) }}</td><td>{{ number(day.unknown_calls) }}</td>
            </tr></tbody>
          </table>
        </div>
      </section>
      <p class="caption">此处是现有模型调用日志统计，不代表支付账单或积分余额。</p>
    </template>
    </div>
    <div id="usage-billing-view" v-show="view === 'billing'">
    <section v-if="billingUsage" class="card billing-card" :aria-busy="loading">
      <div class="card-head"><h2>计费系统记录的个人与企业调用</h2><span>{{ billingUsage.items[0]?.date }} 至 {{ billingUsage.items.at(-1)?.date }} · UTC</span></div>
      <p v-if="billingError" class="note">计费账本刷新失败，当前显示上次成功加载的数据。</p>
      <div class="summary billing-summary">
        <section><span>已知输入 Token</span><strong>{{ number(billingTotals.inputTokens) }}</strong></section>
        <section><span>已知输出 Token</span><strong>{{ number(billingTotals.outputTokens) }}</strong></section>
        <section><span>模型已确认扣费积分</span><strong>{{ credits(billingTotals.chargedMilli) }}</strong></section>
        <section><span>未知用量调用</span><strong>{{ number(billingTotals.unknownCalls) }}</strong></section>
        <section><span>待结算调用</span><strong>{{ number(billingTotals.pendingCalls) }}</strong></section>
      </div>
      <p v-if="billingTotals.searchUnknownCalls || billingTotals.fetchUnknownCalls || billingTotals.searchPendingCalls || billingTotals.fetchPendingCalls"
        class="note" role="status">工具调用有未知或待结算记录，详情见下方。</p>
      <div ref="billingChartElement" class="chart" role="img" aria-label="每日个人和企业空间计费用量趋势；详细数据可在下方表格查看" />
      <details class="billing-details">
        <summary>详细数据</summary>
        <p class="note">只统计当前账号发起且已进入计费账本的调用；未知和待结算用量单独列出，不与调用日志相加。</p>
        <p class="note">“模型内 Search/Fetch”是模型响应附带的工具计数；独立 Search/Fetch 是按成功请求计费的工具。两类分开统计，未知请求冻结额度，明确不计费请求不会扣积分。</p>
        <div class="summary billing-summary">
        <section><span>独立 Search 成功</span><strong>{{ number(billingTotals.searchSuccessCalls) }}</strong></section>
        <section><span>独立 Search 扣费</span><strong>{{ credits(billingTotals.searchChargedMilli) }}</strong></section>
        <section><span>独立 Search 未知（冻结）</span><strong>{{ number(billingTotals.searchUnknownCalls) }} / {{ credits(billingTotals.searchUnknownMilli) }}</strong></section>
        <section><span>独立 Search 待处理（冻结）</span><strong>{{ number(billingTotals.searchPendingCalls) }} / {{ credits(billingTotals.searchPendingMilli) }}</strong></section>
        <section><span>独立 Search 明确不计费</span><strong>{{ number(billingTotals.searchNotBillableCalls) }}</strong></section>
        <section><span>独立 Fetch 成功</span><strong>{{ number(billingTotals.fetchSuccessCalls) }}</strong></section>
        <section><span>独立 Fetch 扣费</span><strong>{{ credits(billingTotals.fetchChargedMilli) }}</strong></section>
        <section><span>独立 Fetch 未知（冻结）</span><strong>{{ number(billingTotals.fetchUnknownCalls) }} / {{ credits(billingTotals.fetchUnknownMilli) }}</strong></section>
        <section><span>独立 Fetch 待处理（冻结）</span><strong>{{ number(billingTotals.fetchPendingCalls) }} / {{ credits(billingTotals.fetchPendingMilli) }}</strong></section>
        <section><span>独立 Fetch 明确不计费</span><strong>{{ number(billingTotals.fetchNotBillableCalls) }}</strong></section>
        <section><span>模型内 Search / Fetch 计数请求</span><strong>{{ number(billingTotals.toolUsageReportedCalls) }}</strong></section>
      </div>
      <p class="note">企业钱包扣费 {{ credits(billingTotals.enterpriseMilli) }} 积分；个人钱包扣费 {{ credits(billingTotals.personalMilli) }} 积分。</p>
      <div class="table-wrap">
        <table>
          <thead><tr><th>日期（UTC）</th><th>输入 Token</th><th>输出 Token</th><th>已知模型调用</th><th>未知模型调用</th><th>待结算模型调用</th><th>独立 Search 成功 / 未知 / 待处理</th><th>Search 扣费 / 不计费</th><th>独立 Fetch 成功 / 未知 / 待处理</th><th>Fetch 扣费 / 不计费</th><th>模型内 Search / Fetch 计数</th><th>模型扣费积分</th></tr></thead>
          <tbody><tr v-for="day in [...billingUsage.items].reverse()" :key="day.date">
            <td>{{ day.date }}</td><td>{{ number(day.input_tokens) }}</td><td>{{ number(day.output_tokens) }}</td>
            <td>{{ number(day.reported_calls) }}</td><td>{{ number(day.unknown_calls) }}</td>
            <td>{{ number(day.pending_calls) }}</td>
            <td>{{ number(day.search_success_calls) }} / {{ number(day.search_unknown_calls) }} / {{ number(day.search_pending_calls) }}</td>
            <td>{{ credits(day.search_charged_milli) }} / {{ number(day.search_not_billable_calls) }}</td>
            <td>{{ number(day.fetch_success_calls) }} / {{ number(day.fetch_unknown_calls) }} / {{ number(day.fetch_pending_calls) }}</td>
            <td>{{ credits(day.fetch_charged_milli) }} / {{ number(day.fetch_not_billable_calls) }}</td>
            <td>{{ number(day.server_tool_search_calls) }} / {{ number(day.server_tool_fetch_calls) }}（{{ number(day.server_tool_usage_reported_calls) }} 次）</td>
            <td>{{ credits(day.charged_milli) }}</td>
          </tr></tbody>
        </table>
      </div>
      <p class="caption">{{ billingUsage.scope_note }}</p>
      </details>
    </section>
    <div v-else-if="billingError" class="notice error" role="alert">
      计费账本用量加载失败。<button type="button" :disabled="loading" @click="loadUsage">重新加载</button>
    </div>
    </div>
  </main>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { getPersonalBillingUsage, getPersonalTokenUsage } from '@/api/personalUsage'
import type { PersonalBillingUsageTrend, PersonalTokenUsageTrend } from '@/api/personalUsage'
import { useUserStore } from '@/stores/user'
import { getToken } from '@/utils/token'

echarts.use([LineChart, GridComponent, TooltipComponent, CanvasRenderer])
const days = ref(30)
const usage = ref<PersonalTokenUsageTrend | null>(null)
const billingUsage = ref<PersonalBillingUsageTrend | null>(null)
const loading = ref(false)
const error = ref(false)
const billingError = ref(false)
const chartElement = ref<HTMLElement | null>(null)
const billingChartElement = ref<HTMLElement | null>(null)
const userStore = useUserStore()
const view = ref<'logs' | 'billing'>('logs')
async function selectView(next: 'logs' | 'billing') {
  view.value = next
  await nextTick()
  if (next === 'logs') chart?.resize()
  else billingChart?.resize()
}
let chart: echarts.EChartsType | null = null
let billingChart: echarts.EChartsType | null = null
let observer: ResizeObserver | null = null
let generation = 0
let identityChangeGeneration = 0
const number = (value: number) => value.toLocaleString('zh-CN')
const credits = (milli: number) => (milli / 1000).toLocaleString('zh-CN', { maximumFractionDigits: 3 })
const billingTotals = computed(() => (billingUsage.value?.items ?? []).reduce((total, day) => ({
  inputTokens: total.inputTokens + day.input_tokens,
  outputTokens: total.outputTokens + day.output_tokens,
  chargedMilli: total.chargedMilli + day.charged_milli,
  unknownCalls: total.unknownCalls + day.unknown_calls,
  pendingCalls: total.pendingCalls + day.pending_calls,
  toolUsageReportedCalls: total.toolUsageReportedCalls + day.server_tool_usage_reported_calls,
  searchSuccessCalls: total.searchSuccessCalls + day.search_success_calls,
  searchChargedMilli: total.searchChargedMilli + day.search_charged_milli,
  searchUnknownCalls: total.searchUnknownCalls + day.search_unknown_calls,
  searchUnknownMilli: total.searchUnknownMilli + day.search_unknown_milli,
  searchPendingCalls: total.searchPendingCalls + day.search_pending_calls,
  searchPendingMilli: total.searchPendingMilli + day.search_pending_milli,
  searchNotBillableCalls: total.searchNotBillableCalls + day.search_not_billable_calls,
  fetchSuccessCalls: total.fetchSuccessCalls + day.fetch_success_calls,
  fetchChargedMilli: total.fetchChargedMilli + day.fetch_charged_milli,
  fetchUnknownCalls: total.fetchUnknownCalls + day.fetch_unknown_calls,
  fetchUnknownMilli: total.fetchUnknownMilli + day.fetch_unknown_milli,
  fetchPendingCalls: total.fetchPendingCalls + day.fetch_pending_calls,
  fetchPendingMilli: total.fetchPendingMilli + day.fetch_pending_milli,
  fetchNotBillableCalls: total.fetchNotBillableCalls + day.fetch_not_billable_calls,
  enterpriseMilli: total.enterpriseMilli + day.enterprise_wallet_charged_milli,
  personalMilli: total.personalMilli + day.personal_wallet_charged_milli,
}), { inputTokens: 0, outputTokens: 0, chargedMilli: 0, unknownCalls: 0, pendingCalls: 0,
  toolUsageReportedCalls: 0,
  searchSuccessCalls: 0, searchChargedMilli: 0, searchUnknownCalls: 0, searchUnknownMilli: 0,
  searchPendingCalls: 0, searchPendingMilli: 0, searchNotBillableCalls: 0,
  fetchSuccessCalls: 0, fetchChargedMilli: 0, fetchUnknownCalls: 0, fetchUnknownMilli: 0,
  fetchPendingCalls: 0, fetchPendingMilli: 0, fetchNotBillableCalls: 0,
  enterpriseMilli: 0, personalMilli: 0 }))

async function loadUsage() {
  const current = ++generation
  const token = getToken()
  const userId = userStore.profile?.id
  const isSameAccount = () => token === getToken() && token === userStore.token && userId === userStore.profile?.id
  loading.value = true
  error.value = false
  billingError.value = false
  const [logs, ledger] = await Promise.allSettled([
    getPersonalTokenUsage(days.value, token), getPersonalBillingUsage(days.value, token),
  ])
  if (current !== generation) return
  if (!isSameAccount()) {
    usage.value = null; billingUsage.value = null; loading.value = false
    chart?.dispose(); chart = null; billingChart?.dispose(); billingChart = null
    return
  }
  if (logs.status === 'fulfilled') usage.value = logs.value
  else error.value = true
  if (ledger.status === 'fulfilled') billingUsage.value = ledger.value
  else billingError.value = true
  await nextTick()
  if (current !== generation) return
  if (!isSameAccount()) {
    usage.value = null; billingUsage.value = null; loading.value = false
    chart?.dispose(); chart = null; billingChart?.dispose(); billingChart = null
    return
  }
  if (usage.value && chartElement.value) {
    chart ??= echarts.init(chartElement.value)
    chart.setOption({
      grid: { left: 58, right: 20, top: 28, bottom: 42 },
      tooltip: { trigger: 'axis' },
      xAxis: { type: 'category', data: usage.value.daily.map(item => item.date.slice(5)), boundaryGap: false },
      yAxis: { type: 'value', minInterval: 1 },
      series: [{ type: 'line', smooth: true, connectNulls: false, data: usage.value.daily.map(item => item.tokens),
        lineStyle: { color: '#5a63db', width: 3 }, itemStyle: { color: '#5a63db' }, areaStyle: { color: 'rgba(90,99,219,.12)' } }],
    })
  }
  if (billingUsage.value && billingChartElement.value) {
    billingChart ??= echarts.init(billingChartElement.value)
    billingChart.setOption({
      grid: { left: 58, right: 20, top: 28, bottom: 42 },
      tooltip: { trigger: 'axis' },
      xAxis: { type: 'category', data: billingUsage.value.items.map(item => item.date.slice(5)), boundaryGap: false },
      yAxis: { type: 'value', minInterval: 1 },
      series: [{ type: 'line', smooth: true, connectNulls: false,
        data: billingUsage.value.items.map(item => item.reported_calls ? item.input_tokens + item.output_tokens
          : item.unknown_calls || item.pending_calls ? null : 0),
        lineStyle: { color: '#239b79', width: 3 }, itemStyle: { color: '#239b79' }, areaStyle: { color: 'rgba(35,155,121,.12)' } }],
    })
  }
  if (!observer && typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(() => { chart?.resize(); billingChart?.resize() })
    if (chartElement.value) observer.observe(chartElement.value)
    if (billingChartElement.value) observer.observe(billingChartElement.value)
  }
  loading.value = false
}
function clearForExpiredSession() {
  generation++
  usage.value = null; billingUsage.value = null; loading.value = false; error.value = false; billingError.value = false
  observer?.disconnect(); observer = null
  chart?.dispose(); chart = null; billingChart?.dispose(); billingChart = null
}
watch(() => [userStore.token, userStore.profile?.id] as const, async ([token, userId], previous) => {
  if (token === previous[0] && userId === previous[1]) return
  const currentIdentityChange = ++identityChangeGeneration
  clearForExpiredSession()
  await nextTick()
  if (currentIdentityChange !== identityChangeGeneration) return
  if (token && userId != null && token === userStore.token && userId === userStore.profile?.id && token === getToken()) {
    void loadUsage()
  }
}, { flush: 'sync' })
onMounted(() => {
  window.addEventListener('prism:auth-expired', clearForExpiredSession)
  void loadUsage()
})
onBeforeUnmount(() => {
  generation++
  window.removeEventListener('prism:auth-expired', clearForExpiredSession)
  observer?.disconnect(); chart?.dispose(); billingChart?.dispose()
})
</script>

<style scoped>
.usage-page { max-width: 1200px; margin: 0 auto; padding: 26px; color: #20243a; }
.page-head, .card-head, .controls { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
h1 { margin: 0; font-size: 24px; } h2 { margin: 0 0 14px; font-size: 16px; }
.page-head p, .caption, .card-head span { color: #69718a; font-size: 13px; }
.usage-views { display:flex; gap:8px; margin-top:20px; }
.usage-views [aria-pressed='true'] { background:#eff0ff; color:#424cc5; border-color:#cfd2ff; }
.billing-details { margin-top:12px; border-top:1px solid #e9eaf3; padding-top:12px; }
.billing-details summary { cursor:pointer; font-size:13px; font-weight:600; }
.billing-details summary:focus-visible, .usage-views button:focus-visible { outline:2px solid #7379ec; outline-offset:3px; }
.controls label { white-space: nowrap; font-size: 13px; }
select, button { border: 1px solid #d9ddeb; background: #fff; border-radius: 8px; color: inherit; padding: 8px 12px; }
button { cursor: pointer; } button:disabled { opacity: .6; cursor: wait; }
.summary { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin: 24px 0 14px; }
.summary section, .card { background: #fff; border: 1px solid #e9eaf3; border-radius: 14px; padding: 20px; }
.summary span { display: block; font-size: 13px; color: #69718a; } .summary strong { display: block; margin-top: 9px; font-size: 25px; }
.token-summary { grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px 20px; margin: 20px 0 14px; }
.summary.token-summary section { min-width: 0; padding: 10px 0; background: transparent; border: 0; border-bottom: 1px solid #e9eaf3; border-radius: 0; }
.token-summary strong { overflow-wrap: anywhere; }
.card { margin-top: 14px; } .card-head h2 { margin: 0; } .chart { width: 100%; height: 260px; }
.note { color: #806128; font-size: 13px; line-height: 1.6; }.notice { margin-top: 24px; padding: 16px; background: #fff; border-radius: 12px; }
.error { color: #a23434; }.table-wrap { overflow-x: auto; max-height: 420px; } table { width: 100%; border-collapse: collapse; text-align: left; font-size: 13px; }
th, td { padding: 10px 12px; border-bottom: 1px solid #eef0f5; white-space: nowrap; } th { color: #69718a; }
@media (max-width: 980px) { .token-summary { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 680px) { .usage-page { padding: 16px; } .page-head { align-items: flex-start; flex-direction: column; } .controls { flex-wrap: wrap; } .summary { grid-template-columns: 1fr; } .summary section { padding: 14px; } .summary.token-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px 16px; } .summary.token-summary section { padding: 10px 0; } }
</style>
