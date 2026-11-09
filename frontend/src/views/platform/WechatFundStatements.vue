<template>
  <section class="fund-workspace" aria-label="微信资金账单">
    <p class="boundary">仅校验文件内汇总，尚未核对订单或余额。</p>
    <div><button type="button" :disabled="loading || busy" @click="loadBatches()">刷新</button></div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <form class="fund-form" @submit.prevent="upload">
      <label>日期<input v-model="billDate" type="date" required :disabled="busy" /></label>
      <label>账户<select v-model="accountType" :disabled="busy"><option value="BASIC">基本账户</option>
        <option value="OPERATION">运营账户</option><option value="FEES">手续费账户</option></select></label>
      <label>文件<input type="file" accept=".csv,.gz,.gzip,application/gzip,text/csv" :disabled="busy" @change="selectFile" /></label>
      <button type="submit" :disabled="busy || !file || !billDate">导入文件</button>
      <button type="button" data-action="download" :disabled="busy || !billDate" @click="download">从微信下载</button>
    </form>
    <p v-if="busy" role="status">正在处理…</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <div class="table-wrap">
      <table v-if="batches.length"><thead><tr><th>日期</th><th>账户</th><th>来源与验证</th><th>记录</th><th>文件摘要</th><th>操作</th></tr></thead>
        <tbody><tr v-for="batch in batches" :key="batch.id">
          <td>{{ batch.bill_date }}</td><td>{{ accountLabel(batch.account_type) }}</td>
          <td>{{ batch.source_origin === 'wechat_api' ? '微信下载 · SHA1 已校验' : '手工导入 · 未验证渠道摘要' }}
            <small v-if="(batch.file_versions ?? 1) > 1">同日账户存在多个文件版本</small></td>
          <td>{{ batch.row_count }} 条</td><td><code>{{ batch.file_sha256.slice(0, 12) }}…</code></td>
          <td><button type="button" :aria-pressed="selected?.id === batch.id" @click="selectBatch(batch)">查看</button></td>
        </tr></tbody></table>
      <p v-else-if="loading" role="status">正在读取…</p><p v-else>暂无资金账单。</p>
    </div>
    <div v-if="total > 50" class="pagination"><button :disabled="offset === 0 || loading" @click="changeBatch(-1)">上一页</button>
      <span>{{ Math.floor(offset / 50) + 1 }} / {{ Math.ceil(total / 50) }}</span>
      <button :disabled="offset + 50 >= total || loading" @click="changeBatch(1)">下一页</button></div>
    <template v-if="selected">
      <div class="summary"><span>收入 {{ money(selected.income_fen) }}</span><span>支出 {{ money(selected.expense_fen) }}</span>
        <span>手续费扣除 {{ money(selected.fee_debit_fen) }}</span><span>手续费退还 {{ money(selected.fee_refund_fen) }}</span></div>
      <div class="table-wrap"><table v-if="rows.length"><thead><tr><th>记账时间</th><th>流水号</th><th>业务</th><th>收支</th><th>金额</th><th>账户结余</th><th>业务凭证</th></tr></thead>
        <tbody><tr v-for="row in rows" :key="row.id"><td>{{ row.booked_at.replace('T', ' ') }}</td>
          <td><code>{{ row.funding_flow_no }}</code></td><td>{{ row.business_type }}</td><td>{{ row.direction }}</td>
          <td>{{ money(row.amount_fen) }}</td><td>{{ money(row.balance_fen) }}</td><td><code>{{ row.business_voucher_no || '—' }}</code></td></tr></tbody></table>
        <p v-else-if="rowsLoading" role="status">正在读取明细…</p><p v-else>该批次没有明细。</p></div>
      <div v-if="selected.row_count > 100" class="pagination"><button :disabled="rowOffset === 0 || rowsLoading" @click="changeRows(-1)">上一页</button>
        <span>{{ Math.floor(rowOffset / 100) + 1 }} / {{ Math.ceil(selected.row_count / 100) }}</span>
        <button :disabled="rowOffset + 100 >= selected.row_count || rowsLoading" @click="changeRows(1)">下一页</button></div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useUserStore } from '@/stores/user'
import { getToken } from '@/utils/token'
import { downloadFundBill, importFundBill, listFundBatches, listFundRows,
  type FundAccount, type FundBatch, type FundRow } from '@/api/wechatFund'

const user = useUserStore()
const yesterday = new Date(); yesterday.setDate(yesterday.getDate() - 1)
const billDate = ref(`${yesterday.getFullYear()}-${String(yesterday.getMonth() + 1).padStart(2, '0')}-${String(yesterday.getDate()).padStart(2, '0')}`)
const accountType = ref<FundAccount>('BASIC'), file = ref<File | null>(null)
const batches = ref<FundBatch[]>([]), rows = ref<FundRow[]>([]), selected = ref<FundBatch | null>(null)
const loading = ref(false), rowsLoading = ref(false), busy = ref(false), error = ref(''), notice = ref('')
const total = ref(0), offset = ref(0), rowOffset = ref(0)
let generation = 0, listSequence = 0, rowSequence = 0, scopeToken = getToken(), invalid = false
let lastRefresh = Number.NEGATIVE_INFINITY

function clear() { batches.value = []; rows.value = []; selected.value = null; total.value = 0; file.value = null }
function invalidate() {
  if (invalid) return
  invalid = true; generation++; listSequence++; rowSequence++; clear()
  loading.value = false; rowsLoading.value = false; busy.value = false; notice.value = ''
  error.value = '登录状态已变化，请重新进入。'
}
function current(request: number) {
  if (!invalid && scopeToken !== getToken()) invalidate()
  return !invalid && request === generation
}
function storage(event: StorageEvent) {
  if ((event.key === 'review_token' || event.key === null) && getToken() !== scopeToken) invalidate()
}
function refreshOnResume() {
  if (document.visibilityState !== 'visible' || !current(generation) || busy.value) return
  const now = Date.now()
  if (now - lastRefresh < 1000) return
  lastRefresh = now
  batches.value = []; rows.value = []; selected.value = null; total.value = 0
  void loadBatches()
}
function accountLabel(value: FundAccount) { return ({ BASIC: '基本', OPERATION: '运营', FEES: '手续费' })[value] }
function money(value: string) {
  if (!/^\d+$/.test(value)) return '—'
  const fen = BigInt(value)
  return `¥${fen / 100n}.${String(fen % 100n).padStart(2, '0')}`
}
async function loadRows(batch: FundBatch) {
  const request = generation, sequence = ++rowSequence
  if (!current(request)) return
  rowsLoading.value = true; rows.value = []
  try {
    const result = await listFundRows(batch.id, scopeToken, rowOffset.value)
    if (current(request) && sequence === rowSequence && selected.value?.id === batch.id) rows.value = result.items
  } catch (cause) {
    if (current(request) && sequence === rowSequence) {
      clear(); error.value = cause instanceof Error ? cause.message : '资金明细读取失败。'
    }
  } finally { if (current(request) && sequence === rowSequence) rowsLoading.value = false }
}
async function loadBatches(preferred?: number) {
  const request = generation, sequence = ++listSequence
  if (!current(request)) return
  loading.value = true
  try {
    const result = await listFundBatches(scopeToken, offset.value)
    if (!current(request) || sequence !== listSequence) return
    batches.value = result.items; total.value = result.total
    const batch = result.items.find(item => item.id === preferred) || result.items[0] || null
    selected.value = batch; rowOffset.value = 0
    if (batch) await loadRows(batch)
    else rows.value = []
  } catch (cause) {
    if (current(request) && sequence === listSequence) { clear(); error.value = cause instanceof Error ? cause.message : '资金账单读取失败。' }
  } finally { if (current(request) && sequence === listSequence) loading.value = false }
}
function selectBatch(batch: FundBatch) { selected.value = batch; rowOffset.value = 0; void loadRows(batch) }
function selectFile(event: Event) {
  const candidate = (event.target as HTMLInputElement).files?.[0] ?? null
  if (candidate && candidate.size > 12 * 1024 * 1024) { file.value = null; error.value = '文件超过 12 MiB。'; return }
  file.value = candidate; error.value = ''
}
async function submit(kind: 'upload' | 'download') {
  const request = generation
  if (!current(request) || busy.value || !billDate.value || (kind === 'upload' && !file.value)) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const result = kind === 'upload'
      ? await importFundBill({ date: billDate.value, account: accountType.value, file: file.value! }, scopeToken)
      : await downloadFundBill({ date: billDate.value, account: accountType.value }, scopeToken)
    if (!current(request)) return
    notice.value = kind === 'upload' ? '文件汇总已核验，渠道摘要未验证。' : '渠道摘要及文件汇总已核验。'
    file.value = null; offset.value = 0; await loadBatches(result.id)
  } catch (cause) { if (current(request)) error.value = cause instanceof Error ? cause.message : '处理失败，请稍后重试。' }
  finally { if (current(request)) busy.value = false }
}
function upload() { void submit('upload') }
function download() { void submit('download') }
function changeBatch(delta: number) { offset.value = Math.max(0, offset.value + delta * 50); void loadBatches() }
function changeRows(delta: number) { rowOffset.value = Math.max(0, rowOffset.value + delta * 100); if (selected.value) void loadRows(selected.value) }
watch(() => [user.profile?.id, user.profile?.role, user.profile?.status, user.token] as const, () => { invalidate() })
onMounted(() => { window.addEventListener('storage', storage); window.addEventListener('focus', refreshOnResume)
  document.addEventListener('visibilitychange', refreshOnResume)
  window.addEventListener('prism:auth-expired', invalidate); void loadBatches() })
onBeforeUnmount(() => { invalidate(); window.removeEventListener('storage', storage); window.removeEventListener('focus', refreshOnResume)
  document.removeEventListener('visibilitychange', refreshOnResume)
  window.removeEventListener('prism:auth-expired', invalidate) })
</script>

<style scoped>
.fund-workspace { display: grid; gap: 14px; }
.boundary, .error { margin: 0; padding: 10px 12px; border-radius: 8px; background: #f4f6f8; color: #4d5866; }
.error { background: #fff1ef; color: #9d2c20; }
.fund-form, .summary, .pagination { display: flex; align-items: end; gap: 12px; flex-wrap: wrap; }
.fund-form label { display: grid; gap: 6px; }
button, input, select { font: inherit; padding: 7px 10px; border: 1px solid #d9dfe7; border-radius: 7px; background: #fff; }
button { cursor: pointer; } button:disabled { cursor: not-allowed; opacity: .55; }
.table-wrap { overflow-x: auto; } table { border-collapse: collapse; width: 100%; min-width: 720px; }
th, td { padding: 10px; text-align: left; border-bottom: 1px solid #edf0f4; vertical-align: top; }
th { color: #65707d; font-weight: 500; } td small { display: block; color: #98662b; }
.summary { gap: 20px; font-weight: 600; } code { overflow-wrap: anywhere; }
@media (max-width: 600px) { .fund-form label { flex: 1 1 160px; } .fund-form button { flex: 1; } }
</style>
