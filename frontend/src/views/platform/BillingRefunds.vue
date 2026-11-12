<template>
  <main class="refund-admin" :aria-busy="loading || busy">
    <header class="page-head">
      <div>
        <h1>计费与退款</h1>
      </div>
      <button type="button" :disabled="loading || busy" @click="load()">刷新</button>
    </header>

    <BillingC4ReviewerManagement kind="refund" />

    <section class="panel filters" aria-label="退款筛选">
      <label class="field">
        <span>退款状态</span>
        <select v-model="status" @change="load()">
          <option v-for="item in statusOptions" :key="item.value" :value="item.value">{{ item.label }}</option>
        </select>
      </label>
      <span class="count">{{ refunds.length }} 条记录</span>
    </section>
    <p v-if="error" class="message error" role="alert">{{ error }}</p>
    <p v-if="notice" class="message notice" role="status">{{ notice }}</p>
    <p v-if="loading && !refunds.length" class="panel empty" role="status">正在加载退款记录…</p>
    <p v-else-if="!loading && !error && refunds.length === 0" class="panel empty">当前筛选下暂无退款记录。</p>

    <section v-if="refunds.length" class="panel table-wrap" aria-label="退款记录">
      <table>
        <thead>
          <tr><th>退款单号</th><th>原订单</th><th>金额</th><th>退款原因</th><th>状态</th><th>渠道状态</th><th>更新时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="refund in refunds" :key="refund.refund_no" :class="{ selected: selected?.refund_no === refund.refund_no }">
            <td><code>{{ refund.refund_no }}</code></td>
            <td><code>{{ refund.order_no }}</code></td>
            <td>{{ money(refund.amount_fen) }}</td>
            <td class="reason-cell">{{ refund.reason }}</td>
            <td>{{ statusLabel(refund.status) }}</td>
            <td>{{ refund.provider_status || '尚未提交渠道' }}</td>
            <td>{{ dateTime(refund.updated_at) }}</td>
            <td><button type="button" :aria-pressed="selected?.refund_no === refund.refund_no" @click="select(refund)">处理</button></td>
          </tr>
        </tbody>
      </table>
    </section>
    <button v-if="hasMore" type="button" data-action="more-refunds" :disabled="loading || busy" @click="load(true)">{{ loading ? '加载中…' : '加载更多' }}</button>

    <section v-if="selected" class="panel detail" aria-live="polite">
      <div class="detail-head">
        <div><h2>退款详情</h2><p><code>{{ selected.refund_no }}</code> · 原订单 <code>{{ selected.order_no }}</code></p></div>
        <strong class="status-pill">{{ statusLabel(selected.status) }}</strong>
      </div>
      <dl>
        <div><dt>退款金额</dt><dd>{{ money(selected.amount_fen) }}</dd></div>
        <div><dt>申请原因</dt><dd>{{ selected.reason }}</dd></div>
        <div><dt>渠道状态</dt><dd>{{ selected.provider_status || '尚未提交渠道' }}</dd></div>
        <div><dt>渠道尝试次数</dt><dd>{{ selected.attempt_count }}</dd></div>
        <div v-if="selected.review_note"><dt>审核/处理记录</dt><dd>{{ selected.review_note }}</dd></div>
      </dl>

      <template v-if="selected.status === 'manual_review' && (selected.requires_reconciliation || ['SUCCESS', 'PROCESSING', 'UNKNOWN', 'NOT_FOUND'].includes(selected.provider_status || ''))">
        <p class="hint recovery-hint">
          <template v-if="selected.provider_status === 'SUCCESS'">支付渠道已报告退款成功，但本地账本尚未完成核验。修复账本问题后重新查单；只有渠道再次确认成功，系统才会重试本地退款入账。</template>
          <template v-else>退款结果仍在渠道处理中或尚未确认。请查单；当前不能批准、拒绝或重复提交退款。</template>
        </p>
        <div class="actions">
          <button type="button" class="primary" :disabled="busy" @click="reconcile">
            {{ selected.provider_status === 'SUCCESS' ? '重新核验渠道并完成本地退款' : '查询渠道退款结果' }}
          </button>
        </div>
      </template>
      <p v-else-if="selected.status === 'manual_review'" role="status">等待独立审核</p>
      <div v-else class="actions">
        <button v-if="canExecute" type="button" class="primary" :disabled="busy" @click="execute">{{ busy ? '处理中…' : '提交/重试渠道退款' }}</button>
        <button v-if="canReconcile" type="button" :disabled="busy" @click="reconcile">查询渠道退款结果</button>
      </div>
      <p v-if="selected.status === 'approved' || selected.status === 'processing' || selected.status === 'unknown'" class="hint">
        退款执行后不得凭页面操作推定成功；未知状态只能查单，不能创建新退款单或释放冻结额度。
      </p>
    </section>
  </main>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useUserStore } from '@/stores/user'
import { getToken } from '@/utils/token'
import BillingC4ReviewerManagement from './BillingC4ReviewerManagement.vue'
import {
  executeBillingRefund, listBillingRefunds, reconcileBillingRefund,
  type BillingRefund, type RefundStatusFilter,
} from '@/api/billing'

const statusOptions: Array<{ value: RefundStatusFilter; label: string }> = [
  { value: 'manual_review', label: '待人工审核' },
  { value: 'requested', label: '待渠道处理' },
  { value: 'approved', label: '已批准' },
  { value: 'processing', label: '渠道处理中' },
  { value: 'unknown', label: '结果未知' },
  { value: 'failed', label: '渠道失败' },
  { value: 'succeeded', label: '已成功' },
  { value: 'rejected', label: '已拒绝' },
]
const status = ref<RefundStatusFilter>('manual_review')
let visibleStatus: RefundStatusFilter = status.value
const refunds = ref<BillingRefund[]>([])
const selected = ref<BillingRefund | null>(null)
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const hasMore = ref(false)
const pageSize = 100
let generation = 0
let accountGeneration = 0
const user = useUserStore()
const currentUserId = computed(() => Number(user.profile?.id ?? 0))
const legacyNeedsQuery = computed(() => ['approved', 'failed'].includes(selected.value?.status ?? '')
  && (Boolean(selected.value?.requires_reconciliation)
    || (selected.value?.provider_status === 'NOT_FOUND' && selected.value.attempt_count > 0)))
const canExecute = computed(() => ['approved', 'requested', 'failed'].includes(selected.value?.status ?? '')
  && !legacyNeedsQuery.value)
const canReconcile = computed(() => ['processing', 'unknown'].includes(selected.value?.status ?? '')
  || legacyNeedsQuery.value)
const money = (fen: number) => `¥${(fen / 100).toFixed(2)}`
const dateTime = (value: string) => new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })
const statusLabel = (value: string) => statusOptions.find(item => item.value === value)?.label || value

async function load(append = false) {
  const current = ++generation
  const account = currentUserId.value, token = getToken()
  if (!account) return
  if (!append && visibleStatus !== status.value) {
    visibleStatus = status.value
    refunds.value = []; selected.value = null; hasMore.value = false
  }
  const beforeId = append ? refunds.value.at(-1)?.id : undefined
  if (append && !beforeId) return
  loading.value = true; error.value = ''; notice.value = ''
  try {
    const rows = await listBillingRefunds(status.value, token, beforeId)
    if (current !== generation || account !== currentUserId.value || token !== getToken()) return
    refunds.value = append ? [...refunds.value, ...rows] : rows
    hasMore.value = rows.length === pageSize && Boolean(rows.at(-1)?.id)
    if (!append) selected.value = selected.value ? rows.find(row => row.refund_no === selected.value?.refund_no) ?? null : null
  } catch {
    if (current === generation && account === currentUserId.value && token === getToken()) {
      error.value = '退款列表加载失败，请确认平台管理员权限后重试。'
    }
  } finally {
    if (current === generation && account === currentUserId.value && token === getToken()) loading.value = false
  }
}
function select(refund: BillingRefund) {
  selected.value = refund
  error.value = ''; notice.value = ''
}
async function runAction(action: (token: string | null) => Promise<BillingRefund>, success: string) {
  const row = selected.value
  if (!row || busy.value) return
  const filter = status.value, current = generation, scope = accountGeneration, account = currentUserId.value, token = getToken()
  busy.value = true; error.value = ''; notice.value = ''
  try {
    await action(token)
    if (current !== generation || account !== currentUserId.value || token !== getToken()) return
    const reload = load(), reloadGeneration = generation
    await reload
    if (scope === accountGeneration && reloadGeneration === generation && account === currentUserId.value
      && token === getToken() && status.value === filter && !error.value) notice.value = success
  } catch (reason) {
    if (current === generation && account === currentUserId.value && token === getToken()) {
      error.value = reason instanceof Error ? reason.message : '退款操作失败；请刷新并查询当前状态。'
    }
  } finally { if (scope === accountGeneration && account === currentUserId.value && token === getToken()) busy.value = false }
}
function execute() {
  const row = selected.value
  if (!row) return
  return runAction(token => executeBillingRefund(row.refund_no, token), '渠道退款请求已处理；请按返回状态继续查单。')
}
function reconcile() {
  const row = selected.value
  if (!row) return
  return runAction(token => reconcileBillingRefund(row.refund_no, token), '已查询渠道退款结果。')
}
onMounted(() => { void load() })
watch([currentUserId, () => user.token], () => {
  generation++; accountGeneration++; refunds.value = []; selected.value = null
  loading.value = false; busy.value = false; error.value = ''; notice.value = ''; hasMore.value = false
  if (currentUserId.value) void load()
}, { flush: 'sync' })
onBeforeUnmount(() => { generation++; accountGeneration++ })
</script>

<style scoped>
.refund-admin { max-width: 1500px; margin: 0 auto; padding: 24px; color: #20243a; }
.page-head, .detail-head, .actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
h1, h2, p { margin-top: 0; }.page-head { margin-bottom: 18px; }.page-head p, .muted, .hint { color: #69718a; font-size: 13px; line-height: 1.55; }
.panel { margin-bottom: 16px; padding: 16px; border: 1px solid #e7e9f1; border-radius: 12px; background: #fff; box-shadow: 0 4px 14px rgb(24 32 56 / 4%); }
.filters { display: flex; align-items: end; justify-content: space-between; }.field { display: grid; gap: 6px; min-width: 240px; color: #434a5d; font-size: 13px; }
select, textarea { box-sizing: border-box; width: 100%; padding: 9px 10px; border: 1px solid #d8dce6; border-radius: 8px; color: inherit; background: white; font: inherit; }
button { min-height: 34px; padding: 7px 12px; border: 1px solid #d8dce6; border-radius: 8px; background: white; color: #30364b; cursor: pointer; }
button:disabled { opacity: .55; cursor: not-allowed; }.primary { border-color: #4f70cb; background: #4f70cb; color: white; }.danger { border-color: #d44b55; color: #bd3340; }
.count { padding-bottom: 8px; color: #6b7283; font-size: 13px; }.empty { color: #69718a; text-align: center; }
.message { padding: 12px 14px; border-radius: 9px; }.error { color: #aa2732; background: #fff1f1; }.notice { color: #256344; background: #edf8f1; }
.table-wrap { overflow-x: auto; padding: 0; }table { width: 100%; border-collapse: collapse; font-size: 12px; white-space: nowrap; }
th, td { padding: 10px 11px; border-bottom: 1px solid #eceef3; text-align: left; vertical-align: top; }th { color: #71798b; background: #fafbfc; font-weight: 600; }
tr.selected { background: #f1f3ff; }.reason-cell { min-width: 170px; max-width: 260px; white-space: normal; }code { color: #50576b; font-size: 11px; }
.detail h2 { margin-bottom: 4px; font-size: 17px; }.detail-head p { margin-bottom: 0; color: #69718a; font-size: 12px; }.status-pill { padding: 5px 9px; border-radius: 20px; color: #555fc5; background: #f0f1ff; font-size: 12px; }
dl { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 9px 22px; margin: 18px 0; }dl div { display: grid; grid-template-columns: 110px 1fr; gap: 8px; }dt { color: #69718a; }dd { margin: 0; overflow-wrap: anywhere; }
.detail textarea { min-height: 78px; resize: vertical; }.actions { justify-content: flex-start; margin-top: 12px; }.hint { margin: 13px 0 0; }.recovery-hint { padding: 10px; border-radius: 8px; color: #7c4b12; background: #fff7e6; }
@media (max-width: 680px) { .refund-admin { padding: 15px; }.filters, .page-head, .detail-head { align-items: flex-start; flex-direction: column; }.field { width: 100%; }.count { padding: 0; }dl { grid-template-columns: 1fr; }dl div { grid-template-columns: 100px 1fr; }.actions { flex-wrap: wrap; } }
</style>
