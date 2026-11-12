<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useUserStore } from '@/stores/user'
import { getToken } from '@/utils/token'
import { decideBillingRefund, listBillingRefundReviews, type BillingRefund } from '@/api/billing'

const user = useUserStore()
const userId = computed(() => Number(user.profile?.id || 0))
const rows = ref<BillingRefund[]>([])
const selected = ref<BillingRefund | null>(null)
const reason = ref('')
const loading = ref(false)
const busy = ref(false)
const denied = ref(false)
const error = ref('')
const message = ref('')
const nextBeforeId = ref<number | null>(null)
let scope = 0
let listRequest = 0

function current(sequence: number, account: number, token: string | null): boolean {
  return sequence === scope && account === userId.value && token === getToken()
}
function clear(): void {
  rows.value = []; selected.value = null; reason.value = ''; loading.value = false
  busy.value = false; denied.value = false; error.value = ''; message.value = ''; nextBeforeId.value = null
}
function fail(cause: unknown, fallback: string): void {
  const code = cause && typeof cause === 'object' && 'code' in cause ? Number(cause.code) : 0
  if (code === 403 || (code >= 40300 && code < 40400)) {
    rows.value = []; selected.value = null; reason.value = ''; denied.value = true; nextBeforeId.value = null
    error.value = '当前账号无退款审核权限。'
  } else error.value = fallback
}
async function load(more = false): Promise<void> {
  const sequence = scope, request = ++listRequest, account = userId.value, token = getToken()
  const cursor = more ? nextBeforeId.value : null
  if (!account || !token || (more && cursor === null)) return
  loading.value = true; error.value = ''; denied.value = false
  try {
    const page = more && cursor !== null ? await listBillingRefundReviews(token, cursor) : await listBillingRefundReviews(token)
    if (!current(sequence, account, token) || request !== listRequest) return
    rows.value = more ? [...rows.value, ...page.items] : page.items
    nextBeforeId.value = page.next_before_id ?? null
    if (!more) { selected.value = null; reason.value = '' }
  } catch (cause) {
    if (current(sequence, account, token) && request === listRequest) fail(cause, '待审核列表加载失败，请重试。')
  } finally { if (current(sequence, account, token) && request === listRequest) loading.value = false }
}
function select(row: BillingRefund): void {
  selected.value = row; reason.value = ''; error.value = ''; message.value = ''
}
async function decide(decision: 'approve' | 'reject'): Promise<void> {
  const row = selected.value, note = reason.value.trim(), sequence = scope, account = userId.value, token = getToken()
  if (!row || !note || !account || !token || loading.value || busy.value) return
  busy.value = true; error.value = ''; message.value = ''
  try {
    await decideBillingRefund(row.refund_no, decision, note, token)
    if (!current(sequence, account, token)) return
    await load()
    if (current(sequence, account, token) && !error.value) message.value = decision === 'approve' ? '审核已批准。' : '退款已拒绝。'
  } catch (cause) {
    if (current(sequence, account, token)) fail(cause, '审核未完成，请刷新并核对当前状态。')
  } finally { if (current(sequence, account, token)) busy.value = false }
}
const money = (fen: number) => `¥${(fen / 100).toFixed(2)}`
const products: Record<string, string> = { reviewer_month: '审查员会员', enterprise_create: '企业席位',
  enterprise_renew: '企业续费', personal_credits: '个人积分', enterprise_credits: '企业积分' }
const product = (value?: BillingRefund['product']) => products[value || ''] || '退款订单'

onMounted(() => { void load() })
watch([userId, () => user.token], () => { ++scope; ++listRequest; clear(); if (userId.value) void load() }, { flush: 'sync' })
onBeforeUnmount(() => { ++scope; ++listRequest })
</script>

<template>
  <main class="reviews" :aria-busy="loading || busy">
    <header><h1>退款待审核</h1><button type="button" data-action="refresh" :disabled="loading || busy" @click="load()">刷新</button></header>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="message" role="status">{{ message }}</p>
    <p v-if="loading && !rows.length" role="status">正在加载…</p>
    <p v-else-if="!rows.length && !denied">暂无待审核退款。</p>
    <div v-if="!denied && rows.length" class="layout">
      <section aria-label="待审核退款">
        <button v-for="row in rows" :key="row.refund_no" type="button" class="review-row"
                :data-refund="row.refund_no" :disabled="loading || busy"
                :aria-pressed="selected?.refund_no === row.refund_no" @click="select(row)">
          <span>{{ product(row.product) }} · {{ money(row.amount_fen) }}</span><small>{{ row.refund_no }}<template v-if="row.status === 'approved'"> · 旧单重新审核</template></small>
        </button>
        <button v-if="nextBeforeId !== null" type="button" data-action="more" :disabled="loading || busy" @click="load(true)">加载更多</button>
      </section>
      <section v-if="selected" aria-label="退款详情">
        <h2>退款详情</h2>
        <p v-if="selected.status === 'approved'" role="status">旧单需重新独立审核，原冻结继续保留。</p>
        <dl>
          <dt>退款单号</dt><dd>{{ selected.refund_no }}</dd>
          <dt>原订单</dt><dd>{{ selected.order_no }}</dd>
          <dt>产品</dt><dd>{{ product(selected.product) }}</dd>
          <dt>金额</dt><dd>{{ money(selected.amount_fen) }}</dd>
          <dt>申请原因</dt><dd>{{ selected.reason }}</dd>
          <dt v-if="selected.review_note">处理记录</dt><dd v-if="selected.review_note">{{ selected.review_note }}</dd>
        </dl>
        <label for="refund-review-reason">审核说明</label>
        <textarea id="refund-review-reason" v-model.trim="reason" maxlength="500" rows="3" :disabled="loading || busy" />
        <div class="actions">
          <button type="button" :disabled="loading || busy || !reason" @click="decide('reject')">拒绝</button>
          <button type="button" data-action="approve" :disabled="loading || busy || !reason" @click="decide('approve')">批准并冻结权益</button>
        </div>
      </section>
    </div>
  </main>
</template>

<style scoped>
.reviews { max-width: 1100px; margin: auto; padding: 24px; }
header, .actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
h1, h2 { margin-top: 0; } .error { color: var(--el-color-danger); }
.layout { display: grid; grid-template-columns: minmax(200px, 300px) minmax(0, 1fr); gap: 20px; }
.review-row { display: block; width: 100%; margin-bottom: 6px; text-align: left; }
.review-row small { display: block; margin-top: 4px; overflow-wrap: anywhere; }
.review-row[aria-pressed="true"] { border-color: var(--el-color-primary); }
dl { display: grid; grid-template-columns: 100px minmax(0, 1fr); gap: 10px; } dd { margin: 0; overflow-wrap: anywhere; }
textarea { display: block; box-sizing: border-box; width: 100%; margin: 8px 0; font: inherit; }
button, textarea { padding: 8px; border: 1px solid var(--el-border-color); border-radius: 6px; background: var(--el-bg-color); color: var(--el-text-color-primary); }
button:focus-visible, textarea:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }
button:disabled { opacity: .5; } .actions { justify-content: flex-start; }
@media (max-width: 760px) { .reviews { padding: 14px; } .layout { grid-template-columns: 1fr; } button { min-height: 44px; } }
</style>
