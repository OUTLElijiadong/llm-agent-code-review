<template>
  <main class="statement-admin" :aria-busy="loading || uploading || reviewing">
    <header class="page-head">
      <div>
        <h1>渠道账单对账</h1>
      </div>
      <button v-if="view === 'trade'" type="button" :disabled="loading || uploading || reviewing" @click="refresh">刷新数据</button>
    </header>

    <nav class="bill-tabs" aria-label="账单类型"><button type="button" :aria-pressed="view === 'trade'" @click="view = 'trade'">交易账单</button>
      <button type="button" :aria-pressed="view === 'fund'" @click="view = 'fund'">微信资金账单</button></nav>
    <WechatFundStatements v-if="view === 'fund'" />
    <template v-else>

    <section class="boundary-note" aria-label="账单处理边界">
      <strong>只导入和审阅，不改账本</strong>
      <span>本页只下载/导入并审阅，不自动结算或调整订单/退款/积分/权益。原始文件解析后即丢弃，只保存文件哈希和必要的归一化字段；渠道返回非 HTTPS 下载链接时拒绝自动下载。</span>
      <span>日期缺失匹配项是基于本地记录时间生成的候选，不能单凭候选判定渠道漏单；支付宝仅接受已适配的 trade CSV 表头，未知格式会拒绝导入。</span>
    </section>

    <p v-if="error" class="message error" role="alert">{{ error }}</p>
    <p v-if="notice" class="message notice" role="status">{{ notice }}</p>

    <section class="panel import-panel" aria-labelledby="import-title">
      <div class="section-title">
        <div><h2 id="import-title">导入渠道账单</h2><p>接受 CSV、单 CSV ZIP 或 GZIP；文件上限 12 MiB。渠道 API 下载受商户配置和安全链接校验约束。</p></div>
        <span class="private-badge">仅平台最高管理员</span>
      </div>
      <form class="import-form" @submit.prevent="importFile">
        <label class="field">
          <span>支付渠道</span>
          <select v-model="channel" :disabled="uploading">
            <option value="wechat">微信支付</option>
            <option value="alipay">支付宝</option>
          </select>
        </label>
        <label class="field">
          <span>账单日期</span>
          <input v-model="billDate" type="date" required :disabled="uploading" />
        </label>
        <label class="field">
          <span>账单类型</span>
          <select v-model="billType" :disabled="uploading">
            <template v-if="channel === 'wechat'">
              <option value="ALL">交易账单（全部）</option>
              <option value="SUCCESS">交易账单（成功）</option>
              <option value="REFUND">退款账单</option>
            </template>
            <option v-else value="trade">支付宝交易账单（trade）</option>
          </select>
        </label>
        <label class="field file-field">
          <span>账单文件</span>
          <input type="file" accept=".csv,.zip,.gz,.gzip,text/csv,application/zip,application/gzip" :disabled="uploading" @change="selectFile" />
          <small>{{ file ? `${file.name} · ${formatBytes(file.size)}` : '选择原始下载文件；不会保留文件副本' }}</small>
        </label>
        <div class="import-actions">
          <button class="primary import-button" type="submit" :disabled="uploading || !file || !billDate">
            {{ uploading ? '解析并匹配中…' : '上传并匹配' }}
          </button>
          <button class="import-button" type="button" :disabled="uploading || !billDate" @click="downloadAndImport">
            {{ uploading ? '下载并匹配中…' : '从渠道 API 下载并匹配' }}
          </button>
        </div>
      </form>
    </section>

    <section class="panel batch-panel" aria-labelledby="batch-title">
      <div class="section-title">
        <div><h2 id="batch-title">已导入批次</h2><p>按日期倒序分页；同一渠道、日期、类型和文件哈希的重复导入会返回原批次。</p></div>
        <span class="count">共 {{ batchTotal }} 个批次</span>
      </div>
      <div v-if="loading && batches.length === 0" class="empty" role="status">正在读取账单批次…</div>
      <div v-else-if="batches.length === 0" class="empty">暂无账单。导入官方账单后会在这里生成对账批次。</div>
      <div v-else class="table-wrap">
        <table>
          <thead><tr><th>日期</th><th>渠道 / 类型</th><th>账单记录</th><th>文件</th><th>导入时间</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="batch in batches" :key="batch.id" :class="{ selected: selectedBatch?.id === batch.id }">
              <td>{{ batch.bill_date }}</td>
              <td>{{ channelLabel(batch.channel) }} · {{ billTypeLabel(batch.bill_type) }}<small>{{ sourceLabel(batch.source_origin) }}</small></td>
              <td>{{ batch.row_count }} 条</td>
              <td class="file-name">{{ batch.file_name }}<small>{{ batch.file_sha256.slice(0, 12) }}… · {{ formatBytes(batch.file_size) }}</small><small v-if="batch.provider_hash_type">渠道摘要 {{ batch.provider_hash_type }} 已校验</small></td>
              <td>{{ dateTime(batch.imported_at) }}</td>
              <td><button type="button" :aria-pressed="selectedBatch?.id === batch.id" @click="selectBatch(batch)">{{ selectedBatch?.id === batch.id ? '当前批次' : '查看对账' }}</button></td>
            </tr>
          </tbody>
        </table>
      </div>
      <footer class="pagination" v-if="batchTotal > BATCH_PAGE_SIZE">
        <span>第 {{ Math.floor(batchOffset / BATCH_PAGE_SIZE) + 1 }} / {{ Math.ceil(batchTotal / BATCH_PAGE_SIZE) }} 页</span>
        <div><button type="button" :disabled="batchOffset === 0 || loading" @click="changeBatchPage(-1)">上一页</button>
          <button type="button" :disabled="batchOffset + BATCH_PAGE_SIZE >= batchTotal || loading" @click="changeBatchPage(1)">下一页</button></div>
      </footer>
    </section>

    <template v-if="selectedBatch">
      <section class="summary-grid" aria-label="批次汇总">
        <article class="metric"><span>账单支付总额</span><strong>{{ money(selectedBatch.payment_total_fen) }}</strong><small>按渠道账单支付记录汇总</small></article>
        <article class="metric"><span>账单退款总额</span><strong>{{ money(selectedBatch.refund_total_fen) }}</strong><small>退款记录单独统计</small></article>
        <article class="metric"><span>渠道支付应结金额</span><strong>{{ money(selectedBatch.received_total_fen) }}</strong><small>仅汇总已提供金额的支付行；{{ selectedBatch.received_missing_count }} 行未提供应结金额</small></article>
        <article class="metric"><span>账单手续费合计</span><strong>{{ feeMoney(selectedBatch.fee_total_micros) }}</strong><small>{{ selectedBatch.fee_missing_count }} 个来源行未提供手续费；保留到百万分之一元，退款行可有符号调整</small></article>
      </section>

      <section class="panel rows-panel" aria-labelledby="rows-title">
        <div class="section-title rows-heading">
          <div><h2 id="rows-title">对账明细</h2><p>{{ selectedBatch.bill_date }} · {{ channelLabel(selectedBatch.channel) }} · {{ billTypeLabel(selectedBatch.bill_type) }} · 共 {{ rowTotal }} 项（包含本地候选）</p></div>
          <button type="button" :disabled="rowsLoading || reviewing" @click="loadRows">刷新明细</button>
        </div>
        <div class="filters" aria-label="明细筛选">
          <label class="field"><span>匹配结果</span>
            <select v-model="discrepancyFilter" @change="applyFilters">
              <option value="">全部</option>
              <option v-for="option in discrepancyOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
            </select>
          </label>
          <label class="field"><span>审阅状态</span>
            <select v-model="reviewFilter" @change="applyFilters">
              <option value="">全部</option><option value="unreviewed">未审阅</option>
              <option value="acknowledge">已知悉</option><option value="investigating">处理中</option>
              <option value="resolved_external">已在渠道侧处理</option>
            </select>
          </label>
          <span class="count">显示 {{ rows.length }} 项</span>
        </div>
        <div v-if="rowsLoading" class="empty" role="status">正在读取对账明细…</div>
        <div v-else-if="rows.length === 0" class="empty">当前筛选下没有明细。</div>
        <div v-else class="table-wrap rows-table">
          <table>
            <thead><tr><th>来源行</th><th>类型</th><th>商户订单号 / 退款号</th><th>渠道交易号</th><th>渠道金额</th><th>应结 / 手续费</th><th>渠道 / 本地状态</th><th>匹配差异</th><th>审阅</th><th>操作</th></tr></thead>
            <tbody>
              <tr v-for="row in rows" :key="row.id" :class="{ selected: selectedRow?.id === row.id }">
                <td>{{ row.source_line_number === 0 ? '本地候选' : `第 ${row.source_line_number} 行` }}</td>
                <td>{{ row.record_type === 'payment' ? '支付' : '退款' }}</td>
                <td><code>{{ row.order_no }}</code><small v-if="row.refund_no"><code>退款 {{ row.refund_no }}</code></small></td>
                <td><code>{{ row.provider_trade_id || '—' }}</code><small v-if="row.provider_refund_id"><code>渠道退款 {{ row.provider_refund_id }}</code></small><small v-if="row.local_provider_refund_id"><code>本地退款 {{ row.local_provider_refund_id }}</code></small></td>
                <td>{{ money(row.amount_fen) }}</td>
                <td>{{ row.received_fen == null ? '—' : money(row.received_fen) }}<small>手续费 {{ row.fee_micros == null ? '—' : feeMoney(row.fee_micros) }}</small></td>
                <td>{{ row.provider_status || '—' }}<small>本地 {{ localStatus(row) }}</small></td>
                <td><span class="status-chip" :class="discrepancyClass(row.discrepancy_code)">{{ discrepancyLabel(row.discrepancy_code) }}</span></td>
                <td>{{ reviewLabel(row.review_status) }}</td>
                <td><button type="button" :aria-pressed="selectedRow?.id === row.id" @click="selectRow(row)">审阅</button></td>
              </tr>
            </tbody>
          </table>
        </div>
        <footer class="pagination" v-if="rowTotal > PAGE_SIZE">
          <span>第 {{ Math.floor(rowOffset / PAGE_SIZE) + 1 }} / {{ Math.ceil(rowTotal / PAGE_SIZE) }} 页</span>
          <div><button type="button" :disabled="rowOffset === 0 || rowsLoading" @click="changePage(-1)">上一页</button>
            <button type="button" :disabled="rowOffset + PAGE_SIZE >= rowTotal || rowsLoading" @click="changePage(1)">下一页</button></div>
        </footer>
      </section>

      <section v-if="selectedRow" class="panel review-panel" aria-labelledby="review-title">
        <div class="section-title"><div><h2 id="review-title">审阅记录</h2><p>{{ discrepancyLabel(selectedRow.discrepancy_code) }} · {{ selectedRow.order_no }}</p></div>
          <button type="button" @click="selectedRow = null">收起</button></div>
        <dl class="review-facts">
          <div><dt>本地订单</dt><dd>{{ selectedRow.local_order_status || '未匹配' }} · {{ selectedRow.local_amount_fen == null ? '—' : money(selectedRow.local_amount_fen) }}</dd></div>
          <div><dt>本地渠道交易号</dt><dd>{{ selectedRow.local_trade_id || '—' }}</dd></div>
          <div><dt>本地退款</dt><dd>{{ selectedRow.local_refund_status || '—' }} · {{ selectedRow.local_refund_amount_fen == null ? '—' : money(selectedRow.local_refund_amount_fen) }}</dd></div>
          <div><dt>最后一次审阅</dt><dd>{{ selectedRow.last_review_note || '暂无' }}</dd></div>
        </dl>
        <label class="field note-field"><span>本次核对说明（必填，最多 500 字）</span>
          <textarea v-model.trim="reviewNote" rows="3" maxlength="500" placeholder="记录核验依据、外部工单号或后续跟进人" />
        </label>
        <div class="review-actions">
          <button type="button" :disabled="reviewing || !reviewNote.trim()" @click="submitReview('acknowledge')">标记已知悉</button>
          <button type="button" :disabled="reviewing || !reviewNote.trim()" @click="submitReview('investigating')">标记处理中</button>
          <button class="primary" type="button" :disabled="reviewing || !reviewNote.trim()" @click="submitReview('resolved_external')">标记渠道侧已处理</button>
          <span v-if="reviewing" role="status">正在保存审阅记录…</span>
        </div>
        <p class="review-boundary">“已在渠道侧处理”只表示人工审阅结论，系统不会因此修改本地支付状态、退款状态、钱包、积分或权益。账本修复需走独立授权流程。</p>
        <div v-if="reviewHistory.length" class="history">
          <h3>历史审阅</h3>
          <ol><li v-for="event in reviewHistory" :key="event.id"><span>{{ reviewLabel(event.decision) }} · 管理员 #{{ event.actor_id }} · {{ dateTime(event.created_at) }}</span><p>{{ event.note }}</p></li></ol>
        </div>
      </section>
    </template>
    </template>
  </main>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useUserStore } from '@/stores/user'
import { getToken } from '@/utils/token'
import WechatFundStatements from './WechatFundStatements.vue'
import {
  fetchBillingStatement, importBillingStatement, listBillingStatements, listBillingStatementRows, listBillingStatementReviews,
  reviewBillingStatementRow,
  type BillingStatementBatch, type BillingStatementReviewDecision, type BillingStatementReviewEvent,
  type BillingStatementRow,
} from '@/api/billing'

const PAGE_SIZE = 100
const view = ref<'trade' | 'fund'>('trade')
const BATCH_PAGE_SIZE = 50
const MAX_FILE_BYTES = 12 * 1024 * 1024
const discrepancyOptions = [
  { value: 'matched', label: '已匹配' },
  { value: 'provider_payment_without_local_order', label: '渠道有支付，本地无订单' },
  { value: 'provider_payment_not_applied', label: '渠道成功，本地未入账' },
  { value: 'payment_channel_mismatch', label: '支付渠道不一致' },
  { value: 'payment_amount_mismatch', label: '支付金额不一致' },
  { value: 'payment_trade_id_mismatch', label: '交易号不一致' },
  { value: 'provider_refund_without_local_order', label: '渠道有退款，本地无订单' },
  { value: 'refund_channel_mismatch', label: '退款渠道不一致' },
  { value: 'provider_refund_without_local_refund', label: '渠道有退款，本地无退款单' },
  { value: 'provider_refund_succeeded_local_pending', label: '渠道退款成功，本地未完成' },
  { value: 'local_refund_completed_provider_not_success', label: '本地退款完成，渠道未成功' },
  { value: 'refund_amount_mismatch', label: '退款金额不一致' },
  { value: 'refund_no_mismatch', label: '退款单号不一致' },
  { value: 'refund_provider_id_mismatch', label: '渠道退款号不一致' },
  { value: 'local_refund_provider_id_missing', label: '本地未记录渠道退款号' },
  { value: 'refund_trade_id_mismatch', label: '退款交易号不一致' },
  { value: 'local_payment_event_missing', label: '本地缺少对应支付事件' },
  { value: 'refund_processing', label: '渠道退款处理中' },
  { value: 'refund_status_mismatch', label: '渠道与本地退款状态不一致' },
  { value: 'provider_refund_status_unknown', label: '渠道退款状态未知' },
  { value: 'unsupported_currency', label: '非人民币币种' },
  { value: 'local_payment_missing_from_statement_candidate', label: '本地有支付、账单无记录（候选）' },
  { value: 'local_refund_missing_from_statement_candidate', label: '本地有退款、账单无记录（候选）' },
]
const channel = ref<BillingStatementBatch['channel']>('wechat')
const localToday = new Date()
localToday.setDate(localToday.getDate() - 1)
const billDate = ref(`${localToday.getFullYear()}-${String(localToday.getMonth() + 1).padStart(2, '0')}-${String(localToday.getDate()).padStart(2, '0')}`)
const billType = ref('ALL')
const file = ref<File | null>(null)
const batches = ref<BillingStatementBatch[]>([])
const batchTotal = ref(0)
const batchOffset = ref(0)
const selectedBatch = ref<BillingStatementBatch | null>(null)
const rows = ref<BillingStatementRow[]>([])
const selectedRow = ref<BillingStatementRow | null>(null)
const reviewHistory = ref<BillingStatementReviewEvent[]>([])
const reviewNote = ref('')
const rowTotal = ref(0)
const rowOffset = ref(0)
const discrepancyFilter = ref('')
const reviewFilter = ref('')
const loading = ref(false)
const rowsLoading = ref(false)
const uploading = ref(false)
const reviewing = ref(false)
const error = ref('')
const notice = ref('')
const user = useUserStore()
const currentUserId = computed(() => Number(user.profile?.id ?? 0))
let batchRequest = 0
let rowsRequest = 0

watch(channel, value => { billType.value = value === 'wechat' ? 'ALL' : 'trade' })

function currentSession(userId: number, token: string | null) {
  return userId === currentUserId.value && token === getToken()
}
function channelLabel(value: string) { return value === 'wechat' ? '微信支付' : value === 'alipay' ? '支付宝' : value }
function sourceLabel(value: string) {
  return ({ manual_upload: '手动上传', alipay_api: '支付宝 API', wechat_api: '微信支付 API' } as Record<string, string>)[value] || value
}
function billTypeLabel(value: string) {
  return ({ ALL: '交易账单（全部）', SUCCESS: '交易账单（成功）', REFUND: '退款账单', trade: '交易账单（trade）' } as Record<string, string>)[value] || value
}
function money(fen: number) { return `¥${(fen / 100).toFixed(2)}` }
function feeMoney(micros: number) { return `¥${(micros / 1_000_000).toFixed(6)}` }
function dateTime(value: string) { return new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) }
function formatBytes(bytes: number) { return bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / 1024 / 1024).toFixed(2)} MiB` }
function reviewLabel(value: string) {
  return ({ unreviewed: '未审阅', acknowledge: '已知悉', investigating: '处理中', resolved_external: '渠道侧已处理' } as Record<string, string>)[value] || value
}
function discrepancyLabel(value: string) {
  return discrepancyOptions.find(item => item.value === value)?.label || value
}
function discrepancyClass(value: string) { return value === 'matched' ? 'success' : value.includes('candidate') ? 'candidate' : 'warning' }
function localStatus(row: BillingStatementRow) {
  return row.record_type === 'payment' ? row.local_order_status || '未匹配' : `${row.local_order_status || '未匹配'} / ${row.local_refund_status || '无退款单'}`
}

function clearWorkspaceState() {
  batchRequest++; rowsRequest++
  batches.value = []; batchTotal.value = 0; batchOffset.value = 0
  selectedBatch.value = null; rows.value = []; selectedRow.value = null
  reviewHistory.value = []; reviewNote.value = ''; rowTotal.value = 0; rowOffset.value = 0
  loading.value = false; rowsLoading.value = false; uploading.value = false; reviewing.value = false
  error.value = ''; notice.value = ''
}

async function loadBatches() {
  const request = ++batchRequest
  const account = currentUserId.value, token = getToken()
  if (!account) return
  loading.value = true; error.value = ''
  try {
    const result = await listBillingStatements(undefined, token, { limit: BATCH_PAGE_SIZE, offset: batchOffset.value })
    if (request !== batchRequest || !currentSession(account, token)) return
    batches.value = result.items
    batchTotal.value = result.total
    const next = batches.value.find(item => item.id === selectedBatch.value?.id) || batches.value[0] || null
    const changed = next?.id !== selectedBatch.value?.id
    selectedBatch.value = next
    if (!next) { rows.value = []; rowTotal.value = 0; selectedRow.value = null }
    else if (changed || rows.value.length === 0) await loadRows()
  } catch (reason) {
    if (request === batchRequest && currentSession(account, token)) error.value = reason instanceof Error ? reason.message : '账单批次加载失败，请确认平台最高管理员权限后重试。'
  } finally {
    if (request === batchRequest && currentSession(account, token)) loading.value = false
  }
}

async function loadRows() {
  const batch = selectedBatch.value
  if (!batch) return
  const request = ++rowsRequest
  const account = currentUserId.value, token = getToken()
  rowsLoading.value = true
  try {
    const result = await listBillingStatementRows(batch.id, {
      ...(discrepancyFilter.value ? { discrepancy_code: discrepancyFilter.value } : {}),
      ...(reviewFilter.value ? { review_status: reviewFilter.value } : {}),
      limit: PAGE_SIZE, offset: rowOffset.value,
    }, token)
    if (request !== rowsRequest || !currentSession(account, token) || selectedBatch.value?.id !== batch.id) return
    rows.value = result.items; rowTotal.value = result.total
    if (selectedRow.value) selectedRow.value = result.items.find(item => item.id === selectedRow.value?.id) || null
  } catch (reason) {
    if (request === rowsRequest && currentSession(account, token)) error.value = reason instanceof Error ? reason.message : '对账明细加载失败。'
  } finally {
    if (request === rowsRequest && currentSession(account, token)) rowsLoading.value = false
  }
}

async function refresh() {
  notice.value = ''
  await loadBatches()
  if (selectedBatch.value) await loadRows()
}
function selectBatch(batch: BillingStatementBatch) {
  selectedBatch.value = batch; selectedRow.value = null; reviewHistory.value = []; reviewNote.value = ''
  rowOffset.value = 0; discrepancyFilter.value = ''; reviewFilter.value = ''
  void loadRows()
}
function applyFilters() { rowOffset.value = 0; selectedRow.value = null; reviewHistory.value = []; void loadRows() }
function changePage(delta: number) { rowOffset.value = Math.max(0, rowOffset.value + delta * PAGE_SIZE); void loadRows() }
function changeBatchPage(delta: number) {
  batchOffset.value = Math.min(Math.max(0, batchOffset.value + delta * BATCH_PAGE_SIZE),
    Math.max(0, Math.ceil(batchTotal.value / BATCH_PAGE_SIZE) - 1) * BATCH_PAGE_SIZE)
  selectedBatch.value = null; selectedRow.value = null; reviewHistory.value = []
  rows.value = []; rowOffset.value = 0; void loadBatches()
}

function selectFile(event: Event) {
  const input = event.target as HTMLInputElement
  const candidate = input.files?.[0] || null
  error.value = ''; notice.value = ''
  if (candidate && candidate.size > MAX_FILE_BYTES) {
    file.value = null; input.value = ''
    error.value = '文件超过 12 MiB。请使用渠道提供的单 CSV ZIP 或 GZIP 文件。'
    return
  }
  file.value = candidate
}

async function importFile() {
  const selectedFile = file.value
  if (!selectedFile || !billDate.value || uploading.value) return
  const account = currentUserId.value, token = getToken()
  if (!account) return
  uploading.value = true; error.value = ''; notice.value = ''
  try {
    const batch = await importBillingStatement({ channel: channel.value, billDate: billDate.value, billType: billType.value, file: selectedFile }, token)
    if (!currentSession(account, token)) return
    file.value = null
    batchOffset.value = 0
    selectedBatch.value = batch
    rowOffset.value = 0; selectedRow.value = null; reviewHistory.value = []; reviewNote.value = ''
    notice.value = `账单已解析并匹配，批次 #${batch.id} 包含 ${batch.row_count} 条渠道记录。原始文件未保存。`
    await loadBatches()
    selectedBatch.value = batch
    await loadRows()
  } catch (reason) {
    if (currentSession(account, token)) error.value = reason instanceof Error ? reason.message : '导入失败。请确认账单日期、渠道和文件格式。'
  } finally {
    if (currentSession(account, token)) uploading.value = false
  }
}

async function downloadAndImport() {
  if (!billDate.value || uploading.value) return
  const account = currentUserId.value, token = getToken()
  if (!account) return
  uploading.value = true; error.value = ''; notice.value = ''
  try {
    const batch = await fetchBillingStatement({ channel: channel.value, billDate: billDate.value, billType: billType.value }, token)
    if (!currentSession(account, token)) return
    batchOffset.value = 0; selectedBatch.value = batch
    rowOffset.value = 0; selectedRow.value = null; reviewHistory.value = []; reviewNote.value = ''
    notice.value = `渠道账单已下载并校验，批次 #${batch.id} 包含 ${batch.row_count} 条记录；账本未被修改。`
    await loadBatches()
    selectedBatch.value = batch
    await loadRows()
  } catch (reason) {
    if (currentSession(account, token)) error.value = reason instanceof Error
      ? reason.message : '渠道下载失败。请检查商户配置、日期和渠道返回的下载链接。'
  } finally {
    if (currentSession(account, token)) uploading.value = false
  }
}

async function selectRow(row: BillingStatementRow) {
  selectedRow.value = row; reviewNote.value = ''; reviewHistory.value = []
  const account = currentUserId.value, token = getToken()
  try {
    const result = await listBillingStatementReviews(row.id, token)
    if (currentSession(account, token) && selectedRow.value?.id === row.id) reviewHistory.value = result.items
  } catch {
    if (currentSession(account, token) && selectedRow.value?.id === row.id) error.value = '审阅历史加载失败；仍可查看当前明细并记录新的审阅。'
  }
}

async function submitReview(decision: BillingStatementReviewDecision) {
  const batch = selectedBatch.value, row = selectedRow.value, note = reviewNote.value.trim()
  if (!batch || !row || !note || reviewing.value) return
  const account = currentUserId.value, token = getToken()
  reviewing.value = true; error.value = ''; notice.value = ''
  try {
    const updated = await reviewBillingStatementRow(batch.id, row.id, decision, note, token)
    if (!currentSession(account, token)) return
    selectedRow.value = updated
    reviewNote.value = ''
    notice.value = `审阅已记录为“${reviewLabel(decision)}”。支付、退款和积分账本未被修改。`
    await Promise.all([loadRows(), selectRow(updated)])
  } catch (reason) {
    if (currentSession(account, token)) error.value = reason instanceof Error ? reason.message : '审阅记录保存失败，请刷新后确认当前状态。'
  } finally {
    if (currentSession(account, token)) reviewing.value = false
  }
}

watch([currentUserId, () => user.token], () => {
  clearWorkspaceState()
  if (currentUserId.value) void loadBatches()
}, { flush: 'sync' })
onMounted(() => {
  window.addEventListener('prism:auth-expired', clearWorkspaceState)
  void loadBatches()
})
onBeforeUnmount(() => {
  clearWorkspaceState()
  window.removeEventListener('prism:auth-expired', clearWorkspaceState)
})
</script>

<style scoped>
.statement-admin { max-width: 1540px; margin: 0 auto; padding: 24px; color: #20243a; }
.page-head, .section-title, .pagination { display: flex; align-items: center; justify-content: space-between; gap: 14px; }
.bill-tabs { display: flex; gap: 8px; margin-bottom: 16px; }
.bill-tabs button[aria-pressed="true"] { color: #3446a5; border-color: #8794db; }
.page-head { margin-bottom: 18px; }.eyebrow { margin: 0 0 5px; color: #6672be; font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
h1, h2, h3, p { margin-top: 0; }h1 { margin-bottom: 6px; font-size: 25px; }h2 { margin-bottom: 4px; font-size: 17px; }h3 { font-size: 14px; }
.intro, .section-title p { margin: 0; color: #69718a; font-size: 13px; line-height: 1.55; }
.panel { margin-bottom: 16px; padding: 17px; border: 1px solid #e7e9f1; border-radius: 13px; background: #fff; box-shadow: 0 4px 14px rgb(24 32 56 / 4%); }
.boundary-note { display: grid; gap: 4px; margin-bottom: 16px; padding: 13px 15px; border: 1px solid #ead9ab; border-radius: 10px; color: #6d531d; background: #fff9e9; font-size: 12px; line-height: 1.55; }
.private-badge, .count { color: #666f86; font-size: 12px; }.private-badge { padding: 5px 9px; border-radius: 18px; color: #555db4; background: #f0f1ff; white-space: nowrap; }
.import-form { display: grid; grid-template-columns: 1fr 1fr 1.3fr 2fr auto; align-items: end; gap: 12px; margin-top: 16px; }
.import-actions { display: grid; gap: 7px; }.import-actions .import-button { width: 100%; }
.field { display: grid; min-width: 0; gap: 6px; color: #434a5d; font-size: 12px; }.field > span { font-weight: 600; }
select, input[type="date"], input[type="file"], textarea { box-sizing: border-box; width: 100%; min-height: 37px; padding: 8px 10px; border: 1px solid #d8dce6; border-radius: 8px; color: inherit; background: #fff; font: inherit; }
.file-field small, td small { display: block; margin-top: 4px; color: #7c8395; font-size: 10px; }.import-button { white-space: nowrap; }
button { min-height: 34px; padding: 7px 12px; border: 1px solid #d8dce6; border-radius: 8px; background: #fff; color: #30364b; font: inherit; font-size: 12px; cursor: pointer; }
button:disabled { opacity: .55; cursor: not-allowed; }button:hover:not(:disabled) { border-color: #7e8bd0; background: #f8f8ff; }.primary { border-color: #4f70cb; background: #4f70cb; color: #fff; }.primary:hover:not(:disabled) { border-color: #405eb4; background: #405eb4; }
.table-wrap { overflow-x: auto; margin-top: 13px; border: 1px solid #eceef3; border-radius: 9px; }table { width: 100%; border-collapse: collapse; font-size: 11px; white-space: nowrap; }
th, td { padding: 9px 10px; border-bottom: 1px solid #eceef3; text-align: left; vertical-align: top; }th { color: #71798b; background: #fafbfc; font-weight: 600; }tr:last-child td { border-bottom: 0; }tr.selected { background: #f2f4ff; }
.file-name { min-width: 145px; max-width: 210px; white-space: normal; overflow-wrap: anywhere; }.file-name small { white-space: nowrap; }code { color: #50576b; font-size: 10px; }
.empty { padding: 26px 12px; color: #737b8c; text-align: center; font-size: 13px; }.message { margin: 12px 0; padding: 12px 14px; border-radius: 9px; font-size: 13px; }.error { color: #aa2732; background: #fff1f1; }.notice { color: #256344; background: #edf8f1; }
.summary-grid { display: grid; grid-template-columns: repeat(4,minmax(0,1fr)); gap: 12px; margin-bottom: 16px; }.metric { display: grid; gap: 6px; padding: 15px; border: 1px solid #e7e9f1; border-radius: 12px; background: #fff; }.metric span, .metric small { color: #71798b; font-size: 11px; }.metric strong { color: #29345c; font-size: 20px; font-variant-numeric: tabular-nums; }.metric small { line-height: 1.4; }
.filters { display: flex; align-items: end; gap: 13px; margin-top: 14px; }.filters .field { width: 230px; }.filters .count { margin-left: auto; padding-bottom: 9px; }.rows-table table { min-width: 1220px; }.rows-panel .section-title { align-items: flex-start; }.pagination { margin-top: 12px; color: #69718a; font-size: 12px; }.pagination div { display: flex; gap: 7px; }
.status-chip { display: inline-block; max-width: 200px; padding: 4px 7px; border-radius: 6px; white-space: normal; line-height: 1.4; }.status-chip.success { color: #226242; background: #e9f7ef; }.status-chip.warning { color: #904b20; background: #fff0e7; }.status-chip.candidate { color: #765b1e; background: #fff7dc; }
.review-panel { border-color: #d8dcf5; }.review-panel .section-title p { margin-top: 4px; }.review-facts { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 9px 22px; margin: 15px 0; }.review-facts div { display: grid; grid-template-columns: 125px 1fr; gap: 8px; font-size: 12px; }.review-facts dt { color: #727a8c; }.review-facts dd { margin: 0; overflow-wrap: anywhere; }.note-field { max-width: 760px; }.note-field textarea { min-height: 76px; resize: vertical; }.review-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-top: 10px; }.review-actions span { color: #69718a; font-size: 12px; }
.review-boundary { margin: 13px 0 0; color: #775b1d; font-size: 12px; line-height: 1.5; }.history { margin-top: 18px; padding-top: 14px; border-top: 1px solid #eceef3; }.history ol { display: grid; gap: 9px; margin: 0; padding-left: 20px; }.history li { color: #69718a; font-size: 11px; }.history li p { margin: 4px 0 0; color: #30364b; font-size: 12px; white-space: pre-wrap; }
@media (max-width: 1050px) { .import-form { grid-template-columns: repeat(2,minmax(0,1fr)); }.file-field { grid-column: span 2; }.import-actions { grid-column: span 2; grid-template-columns: repeat(2,minmax(0,1fr)); } }
@media (max-width: 700px) { .statement-admin { padding: 15px; }.page-head, .section-title { align-items: flex-start; flex-direction: column; }.import-form { grid-template-columns: 1fr; }.file-field { grid-column: auto; }.import-actions { grid-column: auto; grid-template-columns: 1fr; }.import-button { width: 100%; }.summary-grid { grid-template-columns: repeat(2,minmax(0,1fr)); }.filters { align-items: stretch; flex-direction: column; }.filters .field { width: 100%; }.filters .count { margin: 0; padding: 0; }.review-facts { grid-template-columns: 1fr; }.review-facts div { grid-template-columns: 105px 1fr; }.pagination { align-items: flex-start; flex-direction: column; } }
@media (max-width: 420px) { .summary-grid { grid-template-columns: 1fr; }.metric { grid-template-columns: 1fr auto; align-items: center; }.metric small { grid-column: 1 / -1; } }
</style>
