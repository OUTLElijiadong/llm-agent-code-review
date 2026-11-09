<template>
  <section class="usage">
    <div class="heading"><h2>企业 Token 用量</h2><button data-action="refresh-usage" :disabled="loading || !validScope" @click="refreshUsage">刷新</button></div>
    <div class="filters">
      <span class="field"><label for="usage-group">统计范围</label>
      <select id="usage-group" v-model.number="groupId">
        <option v-if="isAdmin" :value="0">整个企业</option>
        <option v-for="group in managedGroups" :key="group.id" :value="group.id">{{ group.name }}</option>
      </select></span>
      <span class="field"><label for="usage-member">成员</label>
      <select id="usage-member" v-model.number="memberId"><option :value="0">全部成员</option>
        <option v-for="member in usageMembers" :key="member.user_id" :value="member.user_id">{{ member.display_name }}</option></select></span>
      <button v-if="directoryHasMore" type="button" @click="emit('load-more-members')">加载更多成员</button>
      <span class="field"><label for="usage-period">时间</label><select id="usage-period" v-model.number="days"><option :value="7">最近 7 天</option><option :value="30">最近 30 天</option><option :value="90">最近 90 天</option></select></span>
    </div>
    <section class="quota" aria-labelledby="quota-heading">
      <div class="section-heading"><h3 id="quota-heading">月度积分额度 <span class="quota-context">已计量调用</span></h3>
        <span class="field"><label for="quota-period">额度月份</label>
        <select id="quota-period" v-model="quotaPeriod"><option v-for="period in quotaPeriods" :key="period" :value="period">{{ period }}</option></select></span>
      </div>
      <p v-if="quotaError" class="error" role="alert">{{ quotaError }}</p>
      <p v-if="quotaStatus" role="status">{{ quotaStatus }}</p>
      <p v-if="quotaPeriodNotice" role="status">{{ quotaPeriodNotice }}</p>
      <form class="quota-form" @submit.prevent="saveLimit">
        <span class="field"><label for="quota-scope">设置范围</label>
        <select id="quota-scope" v-model="quotaScope">
          <option v-if="isAdmin" value="enterprise">整个企业</option>
          <option value="group">小组</option><option value="member">成员</option>
        </select></span>
        <span class="field"><label for="quota-group">目标小组</label>
        <select id="quota-group" v-model.number="quotaGroupId" :disabled="!isAdmin && managedGroups.length === 1">
          <option v-if="isAdmin" :value="0">企业范围</option>
          <option v-for="group in managedGroups" :key="group.id" :value="group.id">{{ group.name }}</option>
        </select></span>
        <span v-if="quotaScope === 'member'" class="field"><label for="quota-member">目标成员</label>
        <select id="quota-member" v-model.number="quotaMemberId">
          <option :value="0">选择成员</option>
          <option v-for="member in quotaMembers" :key="member.user_id" :value="member.user_id">
            {{ member.display_name || `成员 ${member.user_id}` }}
          </option>
        </select></span>
        <span class="field"><label for="quota-credits">每月积分</label>
        <input id="quota-credits" v-model.number="quotaCredits" type="number" min="0" max="100000000" step="1" required @input="quotaPeriodNotice = ''" /></span>
        <button type="submit" :disabled="savingLimit || !canSaveLimit">{{ savingLimit ? '保存中…' : '保存额度' }}</button>
      </form>
      <p v-if="validScope && !quotaLoading && !quotaError && !limits.length" class="muted">该月份尚未设置企业、小组或成员额度。</p>
      <p v-if="quotaLoading" role="status">正在加载月度额度…</p>
      <div v-if="limits.length" class="table-wrap"><table><thead><tr><th>范围</th><th>已用</th><th>预占</th><th>上限</th><th>剩余</th></tr></thead>
        <tbody><tr v-for="limit in limits" :key="`${limit.scope}:${limit.group_id || 0}:${limit.member_id || 0}`">
          <td>{{ limitTitle(limit) }}</td><td>{{ credits(limit.used_milli) }}</td><td>{{ credits(limit.reserved_milli) }}</td>
          <td>{{ credits(limit.limit_milli) }}</td><td>{{ credits(limit.remaining_milli) }}</td>
        </tr></tbody></table></div>
    </section>
    <p v-if="!validScope" class="notice">暂无可查看的小组。</p>
    <p v-if="loading" role="status">正在加载用量…</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <template v-if="usage && !loading">
      <p class="usage-period">已计量调用 · UTC</p>
      <div class="summary"><span>已记录合计：<strong>{{ number(totals.input + totals.output) }}</strong></span>
        <span>模型扣费积分：<strong>{{ credits(totals.modelChargedMilli) }}</strong></span>
        <span>未知用量调用：<strong>{{ number(totals.unknown) }}</strong></span>
        <span>待结算调用：<strong>{{ number(totals.pending) }}</strong></span></div>
      <p v-if="totals.searchUnknownCalls || totals.fetchUnknownCalls || totals.searchPendingCalls || totals.fetchPendingCalls"
        class="pending-tools" role="status">工具调用有未知或待结算记录，详情见下方。</p>
      <div ref="chartElement" class="chart" role="img" aria-label="每日已记录 Token 趋势；详细数据见下表" />
      <details class="usage-details">
        <summary>详细数据</summary>
        <p class="caption">{{ usage.scope_note }}；按 UTC 自然日统计。独立 Search/Fetch 按成功请求计费；模型响应内的工具计数单独展示。未知调用保持额度冻结。</p>
        <div class="summary detail-summary"><span>已记录输入：<strong>{{ number(totals.input) }}</strong></span>
        <span>已记录输出：<strong>{{ number(totals.output) }}</strong></span>
        <span>独立 Search 成功 / 扣费：<strong>{{ number(totals.searchSuccessCalls) }} / {{ credits(totals.searchChargedMilli) }}</strong></span>
        <span>独立 Search 未知（冻结）：<strong>{{ number(totals.searchUnknownCalls) }} / {{ credits(totals.searchUnknownMilli) }}</strong></span>
        <span>独立 Search 待处理（冻结）：<strong>{{ number(totals.searchPendingCalls) }} / {{ credits(totals.searchPendingMilli) }}</strong></span>
        <span>独立 Search 明确不计费：<strong>{{ number(totals.searchNotBillableCalls) }}</strong></span>
        <span>独立 Fetch 成功 / 扣费：<strong>{{ number(totals.fetchSuccessCalls) }} / {{ credits(totals.fetchChargedMilli) }}</strong></span>
        <span>独立 Fetch 未知（冻结）：<strong>{{ number(totals.fetchUnknownCalls) }} / {{ credits(totals.fetchUnknownMilli) }}</strong></span>
        <span>独立 Fetch 待处理（冻结）：<strong>{{ number(totals.fetchPendingCalls) }} / {{ credits(totals.fetchPendingMilli) }}</strong></span>
        <span>独立 Fetch 明确不计费：<strong>{{ number(totals.fetchNotBillableCalls) }}</strong></span>
        <span>模型内工具计数请求：<strong>{{ number(totals.toolUsageReportedCalls) }}</strong></span></div>
      <div class="table-wrap"><table><thead><tr><th>日期（UTC）</th><th>输入</th><th>输出</th><th>模型扣费积分</th><th>未知模型调用</th><th>待结算模型调用</th><th>独立 Search 成功 / 未知 / 待处理</th><th>Search 扣费 / 不计费</th><th>独立 Fetch 成功 / 未知 / 待处理</th><th>Fetch 扣费 / 不计费</th><th>模型内 Search / Fetch（计数请求）</th></tr></thead>
        <tbody><tr v-for="day in [...usage.items].reverse()" :key="day.date"><td>{{ day.date }}</td><td>{{ number(day.input_tokens) }}</td><td>{{ number(day.output_tokens) }}</td><td>{{ credits(day.charged_milli) }}</td><td>{{ number(day.unknown_calls) }}</td><td>{{ number(day.pending_calls) }}</td>
          <td>{{ number(day.search_success_calls) }} / {{ number(day.search_unknown_calls) }} / {{ number(day.search_pending_calls) }}</td>
          <td>{{ credits(day.search_charged_milli) }} / {{ number(day.search_not_billable_calls) }}</td>
          <td>{{ number(day.fetch_success_calls) }} / {{ number(day.fetch_unknown_calls) }} / {{ number(day.fetch_pending_calls) }}</td>
          <td>{{ credits(day.fetch_charged_milli) }} / {{ number(day.fetch_not_billable_calls) }}</td>
          <td>{{ number(day.server_tool_search_calls) }} / {{ number(day.server_tool_fetch_calls) }}（{{ number(day.server_tool_usage_reported_calls) }} 次）</td></tr></tbody></table></div>
      </details>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { getEnterpriseLimits, getEnterpriseUsage, setEnterpriseLimit, type EnterpriseMonthlyLimit, type EnterpriseUsageTrend } from '@/api/personalUsage'
import { listEnterpriseGroupMembers, type EnterpriseDirectoryMember, type EnterpriseGroup, type EnterpriseGroupMember } from '@/api/enterprise'
import { getToken } from '@/utils/token'

echarts.use([LineChart, GridComponent, TooltipComponent, CanvasRenderer])
const props = defineProps<{ enterpriseId: number; role: 'owner' | 'admin' | 'member';
  groups: EnterpriseGroup[]; directory: EnterpriseDirectoryMember[]; directoryHasMore?: boolean }>()
const emit = defineEmits<{ 'access-lost': []; 'load-more-members': [] }>()
const isAdmin = computed(() => props.role === 'owner' || props.role === 'admin')
const managedGroups = computed(() => props.groups.filter(group => group.enterprise_id === props.enterpriseId
  && (isAdmin.value || group.my_role === 'leader')))
const groupId = ref(0), memberId = ref(0), days = ref(30)
const validScope = computed(() => isAdmin.value || managedGroups.value.some(group => group.id === groupId.value))
const usage = ref<EnterpriseUsageTrend | null>(null), loading = ref(false), error = ref('')
const chartElement = ref<HTMLElement | null>(null)
let chart: echarts.EChartsType | null = null
let observer: ResizeObserver | null = null
let generation = 0
const number = (n: number) => n.toLocaleString('zh-CN')
const credits = (milli: number) => (milli / 1000).toLocaleString('zh-CN', { maximumFractionDigits: 3 })
function currentShanghaiPeriod() {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit' }).formatToParts()
  return `${parts.find(part => part.type === 'year')?.value}-${parts.find(part => part.type === 'month')?.value}`
}
function addMonth(period: string) {
  const [year, month] = period.split('-').map(Number)
  return month === 12 ? `${year + 1}-01` : `${year}-${String(month + 1).padStart(2, '0')}`
}
const currentPeriod = ref(currentShanghaiPeriod())
const quotaPeriods = computed(() => [currentPeriod.value, addMonth(currentPeriod.value)])
const quotaPeriod = ref(currentPeriod.value)
const quotaScope = ref<'enterprise' | 'group' | 'member'>('enterprise')
const quotaGroupId = ref(0)
const quotaMemberId = ref(0)
const quotaCredits = ref<number | null>(null)
const quotaLoading = ref(false)
const savingLimit = ref(false)
const quotaError = ref('')
const quotaStatus = ref('')
const quotaPeriodNotice = ref('')
const limits = ref<EnterpriseMonthlyLimit[]>([])
const groupMembers = ref<EnterpriseGroupMember[]>([])
let quotaGeneration = 0
const quotaMembers = computed(() => {
  const source = quotaGroupId.value
    ? groupMembers.value.map(item => ({ user_id: item.user_id,
        display_name: props.directory.find(person => person.user_id === item.user_id)?.display_name }))
    : props.directory
  return source.filter(member => member.user_id !== 0)
})
const usageGroupMembers = ref<EnterpriseGroupMember[]>([])
let usageMemberGeneration = 0
const usageMembers = computed(() => {
  if (!groupId.value && isAdmin.value) return props.directory
  return usageGroupMembers.value.map(item => ({
    user_id: item.user_id,
    display_name: props.directory.find(person => person.user_id === item.user_id)?.display_name || `成员 ${item.user_id}`,
  }))
})
const canSaveLimit = computed(() => (quotaScope.value !== 'member' || quotaMemberId.value > 0)
  && (quotaScope.value !== 'group' || quotaGroupId.value > 0))
const totals = computed(() => (usage.value?.items || []).reduce((sum, item) => ({
  input: sum.input + item.input_tokens, output: sum.output + item.output_tokens,
  modelChargedMilli: sum.modelChargedMilli + item.charged_milli,
  unknown: sum.unknown + item.unknown_calls, pending: sum.pending + item.pending_calls,
  searchSuccessCalls: sum.searchSuccessCalls + item.search_success_calls,
  searchChargedMilli: sum.searchChargedMilli + item.search_charged_milli,
  searchUnknownCalls: sum.searchUnknownCalls + item.search_unknown_calls,
  searchUnknownMilli: sum.searchUnknownMilli + item.search_unknown_milli,
  searchPendingCalls: sum.searchPendingCalls + item.search_pending_calls,
  searchPendingMilli: sum.searchPendingMilli + item.search_pending_milli,
  searchNotBillableCalls: sum.searchNotBillableCalls + item.search_not_billable_calls,
  fetchSuccessCalls: sum.fetchSuccessCalls + item.fetch_success_calls,
  fetchChargedMilli: sum.fetchChargedMilli + item.fetch_charged_milli,
  fetchUnknownCalls: sum.fetchUnknownCalls + item.fetch_unknown_calls,
  fetchUnknownMilli: sum.fetchUnknownMilli + item.fetch_unknown_milli,
  fetchPendingCalls: sum.fetchPendingCalls + item.fetch_pending_calls,
  fetchPendingMilli: sum.fetchPendingMilli + item.fetch_pending_milli,
  fetchNotBillableCalls: sum.fetchNotBillableCalls + item.fetch_not_billable_calls,
  toolUsageReportedCalls: sum.toolUsageReportedCalls + item.server_tool_usage_reported_calls,
}), { input: 0, output: 0, modelChargedMilli: 0, unknown: 0, pending: 0,
  searchSuccessCalls: 0, searchChargedMilli: 0, searchUnknownCalls: 0, searchUnknownMilli: 0,
  searchPendingCalls: 0, searchPendingMilli: 0, searchNotBillableCalls: 0,
  fetchSuccessCalls: 0, fetchChargedMilli: 0, fetchUnknownCalls: 0, fetchUnknownMilli: 0,
  fetchPendingCalls: 0, fetchPendingMilli: 0, fetchNotBillableCalls: 0, toolUsageReportedCalls: 0 }))

async function load() {
  const current = ++generation
  const token = getToken()
  observer?.disconnect(); observer = null; chart?.dispose(); chart = null
  usage.value = null; error.value = ''
  if (!validScope.value) { loading.value = false; return }
  loading.value = true
  try {
    const result = await getEnterpriseUsage(props.enterpriseId, days.value, groupId.value || undefined, memberId.value || undefined, token)
    if (current !== generation || token !== getToken()) return
    usage.value = result; loading.value = false
    await nextTick()
    if (current !== generation || token !== getToken() || !chartElement.value) return
    chart = echarts.init(chartElement.value)
    chart.setOption({ grid: { left: 58, right: 20, top: 28, bottom: 42 }, tooltip: { trigger: 'axis' },
      xAxis: { type: 'category', data: result.items.map(item => item.date.slice(5)), boundaryGap: false },
      yAxis: { type: 'value', minInterval: 1 }, series: [{ type: 'line', smooth: true,
        data: result.items.map(item => item.reported_calls ? item.input_tokens + item.output_tokens
          : (item.unknown_calls || item.pending_calls ? null : 0)) }] })
    if (typeof ResizeObserver !== 'undefined') { observer = new ResizeObserver(() => chart?.resize()); observer.observe(chartElement.value) }
  } catch (cause) {
    if (current !== generation || token !== getToken()) return
    const code = typeof cause === 'object' && cause !== null && 'code' in cause ? Number(cause.code) : 0
    if ((code >= 40300 && code < 40500) || code === 403 || code === 404) {
      error.value = '当前范围的权限已变化。'; emit('access-lost')
    } else error.value = '用量加载失败，请重试。'
  } finally { if (current === generation) loading.value = false }
}
function syncQuotaPeriods(): boolean {
  const period = currentShanghaiPeriod()
  if (period !== currentPeriod.value) currentPeriod.value = period
  if (quotaPeriods.value.includes(quotaPeriod.value)) return true
  quotaPeriod.value = period
  quotaCredits.value = null
  quotaPeriodNotice.value = '月份已更新，请重新输入额度。'
  return false
}
function refreshUsage() {
  const previousPeriod = quotaPeriod.value
  syncQuotaPeriods()
  void load()
  // A period change triggers the existing quota watcher. Otherwise refresh it
  // explicitly, including after a failed request, without issuing two reads.
  if (quotaPeriod.value === previousPeriod) void loadLimits()
  void loadUsageGroupMembers()
}
async function loadLimits() {
  const current = ++quotaGeneration
  const token = getToken()
  limits.value = []; quotaError.value = ''; quotaStatus.value = ''
  groupMembers.value = []
  if (!validScope.value) { quotaLoading.value = false; return }
  quotaLoading.value = true
  try {
    const [result, members] = await Promise.all([
      getEnterpriseLimits(props.enterpriseId, quotaPeriod.value, isAdmin.value ? undefined : quotaGroupId.value, token),
      quotaGroupId.value ? listEnterpriseGroupMembers(props.enterpriseId, quotaGroupId.value, token) : Promise.resolve([]),
    ])
    if (current !== quotaGeneration || token !== getToken()) return
    limits.value = result.items
    groupMembers.value = members
  } catch (cause) {
    if (current !== quotaGeneration || token !== getToken()) return
    const code = typeof cause === 'object' && cause !== null && 'code' in cause ? Number(cause.code) : 0
    if ((code >= 40300 && code < 40500) || code === 403 || code === 404) {
      quotaError.value = '当前范围的权限已变化。'; emit('access-lost')
    } else quotaError.value = '月度额度加载失败，请重试。'
  } finally { if (current === quotaGeneration) quotaLoading.value = false }
}
async function saveLimit() {
  if (!syncQuotaPeriods()) return
  if (savingLimit.value || !canSaveLimit.value || quotaCredits.value === null) return
  const requestGeneration = quotaGeneration
  const token = getToken()
  const requestEnterpriseId = props.enterpriseId
  const requestScope = quotaScope.value
  const requestPeriod = quotaPeriod.value
  const requestGroupId = requestScope === 'enterprise' ? 0 : quotaGroupId.value
  const requestMemberId = requestScope === 'member' ? quotaMemberId.value : 0
  const requestTargetName = requestScope === 'member'
    ? (props.directory.find(member => member.user_id === requestMemberId)?.display_name || `成员 ${requestMemberId}`)
    : requestGroupId ? (props.groups.find(group => group.id === requestGroupId)?.name || '上一项') : '企业'
  const sameTarget = () => requestEnterpriseId === props.enterpriseId && requestScope === quotaScope.value
    && requestPeriod === quotaPeriod.value && (requestScope === 'enterprise' || requestGroupId === quotaGroupId.value)
    && (requestScope !== 'member' || requestMemberId === quotaMemberId.value)
  const sameSelection = () => requestGeneration === quotaGeneration && sameTarget()
  savingLimit.value = true; quotaError.value = ''; quotaStatus.value = ''
  try {
    const groupIdForLimit = requestGroupId || undefined
    await setEnterpriseLimit(props.enterpriseId, {
      period: requestPeriod, scope: requestScope, limit_credits: quotaCredits.value,
      group_id: groupIdForLimit,
      member_id: requestMemberId || undefined,
    }, token)
    if (token !== getToken()) return
    if (sameSelection()) {
      await loadLimits()
      // The save is already confirmed, but its subsequent refresh may finish
      // after the user has selected another scope. Report the original target.
      if (token === getToken() && requestEnterpriseId === props.enterpriseId && !sameTarget()) {
        quotaStatus.value = `${requestTargetName}的额度已保存（${requestPeriod}）。`
      }
    } else if (requestEnterpriseId === props.enterpriseId) quotaStatus.value = `${requestTargetName}的额度已保存（${requestPeriod}）。`
  } catch (cause) {
    if (token !== getToken()) return
    if (!sameSelection()) {
      if (requestEnterpriseId === props.enterpriseId) {
        quotaError.value = `${requestTargetName}的额度保存结果未知，请切换回原范围刷新核对（${requestPeriod}）。`
      }
      return
    }
    const code = typeof cause === 'object' && cause !== null && 'code' in cause ? Number(cause.code) : 0
    if ((code >= 40300 && code < 40500) || code === 403 || code === 404) {
      quotaError.value = '当前范围的权限已变化。'; emit('access-lost')
    } else quotaError.value = '额度保存失败，请刷新确认后重试。'
  } finally { savingLimit.value = false }
}
function limitTitle(limit: EnterpriseMonthlyLimit) {
  if (limit.scope === 'enterprise') return '整个企业'
  if (limit.scope === 'group') return props.groups.find(group => group.id === limit.group_id)?.name || `小组 ${limit.group_id}`
  const member = props.directory.find(person => person.user_id === limit.member_id)
  return member?.display_name || `成员 ${limit.member_id}`
}
async function loadUsageGroupMembers() {
  const current = ++usageMemberGeneration
  const token = getToken()
  usageGroupMembers.value = []
  if (!validScope.value || !groupId.value) return
  try {
    const members = await listEnterpriseGroupMembers(props.enterpriseId, groupId.value, token)
    if (current === usageMemberGeneration && token === getToken()) usageGroupMembers.value = members
  } catch (cause) {
    if (current !== usageMemberGeneration || token !== getToken()) return
    const code = typeof cause === 'object' && cause !== null && 'code' in cause ? Number(cause.code) : 0
    if ((code >= 40300 && code < 40500) || code === 403 || code === 404) emit('access-lost')
    else error.value = '小组成员加载失败，请重试。'
  }
}
watch([() => props.enterpriseId, () => props.role, managedGroups], () => {
  if (!isAdmin.value && !managedGroups.value.some(group => group.id === groupId.value)) groupId.value = managedGroups.value[0]?.id || 0
  if (isAdmin.value && groupId.value && !managedGroups.value.some(group => group.id === groupId.value)) groupId.value = 0
}, { immediate: true, flush: 'sync' })
watch([() => props.enterpriseId, groupId, memberId, days, validScope], () => { void load() }, { immediate: true })
watch([() => props.enterpriseId, () => props.role, quotaPeriod, quotaGroupId, validScope], () => { void loadLimits() }, { immediate: true })
watch(quotaGroupId, () => { quotaMemberId.value = 0 })
watch([() => props.enterpriseId, () => props.role, managedGroups], ([enterpriseId, role], previous) => {
  const firstLoad = !previous?.length
  const identityChanged = !firstLoad && (previous[0] !== enterpriseId || previous[1] !== role)
  if (firstLoad || identityChanged) {
    quotaScope.value = isAdmin.value ? 'enterprise' : 'group'
    quotaGroupId.value = isAdmin.value ? 0 : (managedGroups.value[0]?.id || 0)
    quotaMemberId.value = 0
    if (identityChanged) { quotaCredits.value = null; quotaPeriodNotice.value = '' }
    return
  }
  if (quotaGroupId.value && !managedGroups.value.some(group => group.id === quotaGroupId.value)) {
    if (quotaScope.value !== 'enterprise') quotaCredits.value = null
    quotaMemberId.value = 0
    quotaGroupId.value = isAdmin.value ? 0 : (managedGroups.value[0]?.id || 0)
  } else if (!isAdmin.value && !quotaGroupId.value && managedGroups.value.length) {
    quotaGroupId.value = managedGroups.value[0].id
  }
}, { immediate: true, flush: 'sync' })
watch([() => props.enterpriseId, () => props.role, groupId, validScope], () => {
  memberId.value = 0
  void loadUsageGroupMembers()
}, { immediate: true })
watch(quotaScope, value => { if (value !== 'member') quotaMemberId.value = 0 })
onMounted(() => {
  window.addEventListener('focus', syncQuotaPeriods)
  document.addEventListener('visibilitychange', syncQuotaPeriods)
})
onBeforeUnmount(() => {
  window.removeEventListener('focus', syncQuotaPeriods)
  document.removeEventListener('visibilitychange', syncQuotaPeriods)
  ++generation; ++quotaGeneration; ++usageMemberGeneration; observer?.disconnect(); chart?.dispose()
})
</script>

<style scoped>
.usage { padding: 20px; background: white; border: 1px solid #e9eaf3; border-radius: 14px; }
.heading,.filters,.summary,.section-heading,.quota-form { display:flex; align-items:center; flex-wrap:wrap; gap:12px; } .heading,.section-heading { justify-content:space-between; }
.field { display:inline-flex; align-items:center; gap:8px; max-width:100%; } .field label { white-space:nowrap; } .field select,.field input { min-width:0; max-width:100%; }
h2 { font-size: 16px; margin:0; }.notice,.caption { color:#69718a; font-size:13px; line-height:1.6; }
.usage-period { color:#69718a; font-size:12px; margin:20px 0 8px; }
.usage-details { margin-top:12px; border-top:1px solid #e9eaf3; padding-top:12px; }
.usage-details summary { cursor:pointer; font-size:13px; font-weight:600; }
.usage-details summary:focus-visible { outline:2px solid #7379ec; outline-offset:3px; }
.pending-tools { color:#806128; font-size:13px; }
select,button,input { border:1px solid #d9ddeb; background:white; border-radius:8px; padding:8px 10px; color:inherit; }
.filters { margin: 18px 0; font-size: 13px; }.summary { gap:18px; font-size:13px; }.summary strong { color:#3946a3; }
.quota { margin:20px 0; padding:16px; border:1px solid #e9eaf3; border-radius:12px; }.quota h3 { font-size:15px; margin:0; }
.quota-context { color:#69718a; font-size:12px; font-weight:400; }
.quota-form { margin:14px 0; }.quota-form label { font-size:13px; }.quota-form input { width:140px; }
.chart { width:100%; height:260px; }.table-wrap { overflow:auto; max-height:420px; } table { border-collapse:collapse; width:100%; text-align:left; font-size:13px; }
th,td { padding:10px; border-bottom:1px solid #eef0f5; white-space:nowrap; }.error { color:#a23434; }
button:focus-visible, select:focus-visible, input:focus-visible { outline:2px solid #7379ec; outline-offset:2px; }
</style>
