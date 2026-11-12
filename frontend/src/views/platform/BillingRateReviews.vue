<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useUserStore } from '@/stores/user'
import { getToken } from '@/utils/token'
import { decideBillingRateReview, getBillingRateReview, listBillingRateReviews, listBillingRateCancellations, decideBillingRateCancellation, type BillingRateCatalog, type BillingRateCancellationReview } from '@/api/billing'
const user = useUserStore()
const userId = computed(() => Number(user.profile?.id || 0))
const rows = ref<BillingRateCatalog[]>([])
const selected = ref<BillingRateCatalog | null>(null)
const cancellations = ref<BillingRateCancellationReview[]>([])
const selectedCancel = ref<BillingRateCancellationReview | null>(null)
const note = ref(''), error = ref(''), message = ref('')
const denied = ref(false), loading = ref(false), busy = ref(false)
let generation = 0
function current(seq: number, id: number, token: string | null) { return generation === seq && id === userId.value && token === getToken() }
function fail(cause: unknown, fallback: string) {
  const code = cause && typeof cause === 'object' && 'code' in cause ? Number(cause.code) : 0
  if (code === 403 || (code >= 40300 && code < 40400)) {
    rows.value = []; selected.value = null; cancellations.value = []; selectedCancel.value = null; note.value = ''; denied.value = true; error.value = '当前账号无费率复核权限，请联系平台最高管理员。'
  } else error.value = fallback
}
async function load() {
  const seq = ++generation, id = userId.value, token = getToken()
  if (!id || !token) return
  loading.value = true; error.value = ''; denied.value = false
  try {
    const [data, cancelData] = await Promise.all([listBillingRateReviews(token), listBillingRateCancellations(token)])
    if (!current(seq, id, token)) return
    rows.value = data.items; cancellations.value = cancelData.items; selected.value = null; selectedCancel.value = null; note.value = ''
  } catch (cause) { if (current(seq, id, token)) fail(cause, '待审费率加载失败，请重试。') }
  finally { if (current(seq, id, token)) loading.value = false }
}
async function open(id: number) {
  const seq = ++generation, account = userId.value, token = getToken()
  if (!account || !token || busy.value) return
  selected.value = null; selectedCancel.value = null; note.value = ''; error.value = ''; message.value = ''; loading.value = true
  try { const data = await getBillingRateReview(id, token); if (current(seq, account, token)) selected.value = data }
  catch (cause) { if (current(seq, account, token)) fail(cause, '草稿已处理或无法读取，请刷新列表。') }
  finally { if (current(seq, account, token)) loading.value = false }
}
async function decide(decision: 'approve' | 'reject') {
  const row = selected.value, id = userId.value, token = getToken(), reason = note.value.trim(), seq = generation
  if (!row || !id || !token || busy.value || row.created_by === id || (decision === 'reject' && !reason)) return
  busy.value = true; error.value = ''; message.value = ''
  try {
    await decideBillingRateReview(row.id, decision, reason, token)
    if (!current(seq, id, token)) return
    await load()
    if (id === userId.value && token === getToken()) message.value = decision === 'approve' ? '费率已批准，将按计划生效。' : '草稿已拒绝。'
  } catch (cause) { if (current(seq, id, token)) fail(cause, '操作未完成，请刷新核对状态。') }
  finally { if (id === userId.value && token === getToken()) busy.value = false }
}
function openCancellation(item: BillingRateCancellationReview) {
  if (busy.value || loading.value) return
  ++generation; selected.value = null; selectedCancel.value = item; note.value = ''; error.value = ''; message.value = ''
}
async function decideCancellation(decision: 'approve' | 'reject') {
  const row = selectedCancel.value, id = userId.value, token = getToken(), reason = note.value.trim(), seq = generation
  if (!row || !id || !token || !reason || busy.value || row.proposed_by === id) return
  busy.value = true; error.value = ''; message.value = ''
  try {
    const result = await decideBillingRateCancellation(row.id, decision, reason, token)
    if (!current(seq, id, token)) return
    await load()
    if (id === userId.value && token === getToken()) message.value = result.status === 'expired' ? '申请已过期，费率未取消。'
      : result.status === 'approved' ? '取消申请已批准。' : '取消申请已拒绝。'
  } catch (cause) { if (current(seq, id, token)) fail(cause, '操作未完成，请刷新核对状态。') }
  finally { if (id === userId.value && token === getToken()) busy.value = false }
}
function date(value: string | null) { return value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '未核验' }
function margin(value: number | null | undefined) { return value == null ? '未核验' : `${(value / 100).toFixed(2)}%` }
onMounted(() => { void load() })
watch([userId, () => user.token], () => { ++generation; rows.value = []; selected.value = null; cancellations.value = []; selectedCancel.value = null; note.value = ''; error.value = ''; message.value = ''; loading.value = false; busy.value = false; denied.value = false; if (userId.value) void load() }, { flush: 'sync' })
onBeforeUnmount(() => { ++generation })
</script>
<template>
  <main class="reviews" :aria-busy="loading || busy">
    <header><h1>费率待复核</h1><button type="button" data-action="refresh" :disabled="loading || busy" @click="load">刷新</button></header>
    <p v-if="error" role="alert" class="error">{{ error }}</p><p v-if="message" role="status">{{ message }}</p>
    <p v-if="loading && !rows.length" role="status">正在加载…</p><p v-else-if="!rows.length && !cancellations.length && !denied">暂无待审草稿。</p>
    <div v-if="!denied && (rows.length || cancellations.length)" class="layout">
      <section aria-label="待审费率"><button v-for="row in rows" :key="row.id" type="button" class="review-row" :data-review="row.id" :aria-pressed="selected?.id === row.id" :disabled="loading || busy" @click="open(row.id)">#{{ row.id }} · {{ row.version_key.slice(0, 12) }}</button><h3 v-if="cancellations.length">取消申请</h3><button v-for="item in cancellations" :key="item.id" type="button" class="review-row" :data-cancellation="item.id" :aria-pressed="selectedCancel?.id === item.id" :disabled="loading || busy" @click="openCancellation(item)">申请 #{{ item.id }} · 费率 #{{ item.catalog_id }}</button></section>
      <section v-if="selected" aria-label="费率详情">
        <h2>费率 #{{ selected.id }}</h2>
        <dl>
          <dt>变更说明</dt><dd>{{ selected.change_note }}</dd><dt>计划生效</dt><dd>{{ date(selected.effective_at) }}（北京时间）</dd>
          <dt>创建人</dt><dd>#{{ selected.created_by }}</dd><dt>版本摘要</dt><dd>{{ selected.version_key }}</dd>
          <dt>价格事实摘要</dt><dd>{{ selected.source_sha256 }}</dd><dt>积分收入基准</dt><dd>¥{{ (selected.credit_pack_price_fen / 100).toFixed(2) }} / {{ selected.credit_pack_credits }} 积分</dd>
          <dt>汇率</dt><dd>¥{{ selected.fx_cny_per_usd }} / USD</dd>
          <dt>美元来源</dt><dd>{{ selected.source_url }} · {{ date(selected.source_checked_at) }}</dd>
          <dt>人民币来源</dt><dd>{{ selected.model_cny_source_url || '未核验' }} · {{ date(selected.model_cny_source_checked_at) }}</dd>
          <dt>汇率来源</dt><dd>{{ selected.fx_source_url }} · {{ date(selected.fx_source_checked_at) }}</dd>
          <dt>工具人民币来源</dt><dd>{{ selected.tool_cny_source_url || '未启用' }} · {{ date(selected.tool_cny_source_checked_at) }}</dd>
          <dt>工具美元来源</dt><dd>{{ selected.tool_usd_source_url || '未启用' }} · {{ date(selected.tool_usd_source_checked_at) }}</dd>
        </dl>
        <h3>模型价格</h3>
        <div v-for="item in selected.items" :key="item.model_name" class="model">
          <strong>{{ item.model_name }}</strong>
          <p>用户费率（毫积分 / Token）：输入 {{ item.input_rate_numerator }}/{{ item.input_rate_denominator }}；输出 {{ item.output_rate_numerator }}/{{ item.output_rate_denominator }}</p>
          <p>供应商价格（微美元 / 百万 Token）：输入 {{ item.provider_input_usd_micros_per_million }}；输出 {{ item.provider_output_usd_micros_per_million }}</p>
          <p>供应商价格（人民币微元 / 百万 Token）：输入 {{ item.provider_input_cny_micros_per_million ?? '未核验' }}；输出 {{ item.provider_output_cny_micros_per_million ?? '未核验' }}</p>
          <p>直采毛利：输入 {{ margin(selected.margin_bps[item.model_name]?.input_bps) }}；输出 {{ margin(selected.margin_bps[item.model_name]?.output_bps) }}</p>
        </div>
        <h3>工具价格</h3><p>Search：成本 {{ selected.tool_search_cost_cny_micros_per_success }} 人民币微元 / 次；用户价 {{ selected.tool_search_price_milli_per_success }} 毫积分 / 次；毛利 {{ margin(selected.tool_margin_bps.search) }}</p>
        <p>Fetch：成本 {{ selected.tool_fetch_cost_cny_micros_per_success }} 人民币微元 / 次；用户价 {{ selected.tool_fetch_price_milli_per_success }} 毫积分 / 次；毛利 {{ margin(selected.tool_margin_bps.fetch) }}</p>
        <p>供应商模型成本上涨 {{ selected.supplier_price_diff.increases.length }} 项，下降 {{ selected.supplier_price_diff.decreases.length }} 项；工具成本上涨 {{ selected.tool_supplier_price_diff.increases.length }} 项，下降 {{ selected.tool_supplier_price_diff.decreases.length }} 项。</p>
        <p v-if="selected.created_by === userId" role="status">不能复核自己的草稿。</p>
        <template v-else><label for="rate-review-note">拒绝原因</label><textarea id="rate-review-note" v-model="note" maxlength="500" rows="3" :disabled="busy" />
          <div class="actions"><button type="button" :disabled="busy || !note.trim()" @click="decide('reject')">拒绝</button><button type="button" data-action="approve" :disabled="busy" @click="decide('approve')">批准</button></div>
        </template>
      </section>
      <section v-if="selectedCancel" aria-label="取消申请详情">
        <h2>取消申请 #{{ selectedCancel.id }}</h2>
        <dl>
          <dt>申请人</dt><dd>#{{ selectedCancel.proposed_by }}</dd><dt>取消原因</dt><dd>{{ selectedCancel.proposal_note }}</dd>
          <dt>版本</dt><dd>{{ selectedCancel.catalog.version_key }}</dd><dt>计划生效</dt><dd>{{ date(selectedCancel.catalog.effective_at) }}（北京时间）</dd>
          <dt>版本说明</dt><dd>{{ selectedCancel.catalog.change_note }}</dd><dt>价格事实摘要</dt><dd>{{ selectedCancel.catalog.source_sha256 }}</dd>
          <dt>积分收入基准</dt><dd>¥{{ (selectedCancel.catalog.credit_pack_price_fen / 100).toFixed(2) }} / {{ selectedCancel.catalog.credit_pack_credits }} 积分</dd>
          <dt>汇率</dt><dd>¥{{ selectedCancel.catalog.fx_cny_per_usd }} / USD</dd>
          <dt>美元来源</dt><dd>{{ selectedCancel.catalog.source_url }} · {{ date(selectedCancel.catalog.source_checked_at) }}</dd>
          <dt>人民币来源</dt><dd>{{ selectedCancel.catalog.model_cny_source_url || '未核验' }} · {{ date(selectedCancel.catalog.model_cny_source_checked_at) }}</dd>
          <dt>汇率来源</dt><dd>{{ selectedCancel.catalog.fx_source_url }} · {{ date(selectedCancel.catalog.fx_source_checked_at) }}</dd>
          <dt>工具人民币来源</dt><dd>{{ selectedCancel.catalog.tool_cny_source_url || '未启用' }} · {{ date(selectedCancel.catalog.tool_cny_source_checked_at) }}</dd>
          <dt>工具美元来源</dt><dd>{{ selectedCancel.catalog.tool_usd_source_url || '未启用' }} · {{ date(selectedCancel.catalog.tool_usd_source_checked_at) }}</dd>
        </dl>
        <h3>模型价格</h3><div v-for="item in selectedCancel.catalog.items" :key="item.model_name" class="model">
          <strong>{{ item.model_name }}</strong>
          <p>用户费率（毫积分 / Token）：输入 {{ item.input_rate_numerator }}/{{ item.input_rate_denominator }}；输出 {{ item.output_rate_numerator }}/{{ item.output_rate_denominator }}</p>
          <p>供应商价格（微美元 / 百万 Token）：输入 {{ item.provider_input_usd_micros_per_million }}；输出 {{ item.provider_output_usd_micros_per_million }}</p>
          <p>供应商价格（人民币微元 / 百万 Token）：输入 {{ item.provider_input_cny_micros_per_million ?? '未核验' }}；输出 {{ item.provider_output_cny_micros_per_million ?? '未核验' }}</p>
        </div>
        <h3>工具价格</h3><p>Search：成本 {{ selectedCancel.catalog.tool_search_cost_cny_micros_per_success }} 人民币微元 / 次；用户价 {{ selectedCancel.catalog.tool_search_price_milli_per_success }} 毫积分 / 次</p>
        <p>Fetch：成本 {{ selectedCancel.catalog.tool_fetch_cost_cny_micros_per_success }} 人民币微元 / 次；用户价 {{ selectedCancel.catalog.tool_fetch_price_milli_per_success }} 毫积分 / 次</p>
        <p v-if="selectedCancel.proposed_by === userId" role="status">不能复核自己的取消申请。</p>
        <template v-else><label for="rate-cancel-note">复核说明</label><textarea id="rate-cancel-note" v-model="note" maxlength="500" rows="3" :disabled="busy" />
          <div class="actions"><button type="button" :disabled="busy || !note.trim()" @click="decideCancellation('reject')">拒绝取消</button><button type="button" data-action="approve-cancel" :disabled="busy || !note.trim()" @click="decideCancellation('approve')">批准取消</button></div>
        </template>
      </section>
    </div>
  </main>
</template>
<style scoped>
.reviews { max-width: 1100px; margin: auto; padding: 24px; } header, .actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
h1, h2 { margin-top: 0; } .error { color: var(--el-color-danger); } .layout { display: grid; grid-template-columns: minmax(180px, 260px) minmax(0, 1fr); gap: 20px; }
.review-row { display: block; width: 100%; margin-bottom: 6px; text-align: left; } .review-row[aria-pressed="true"] { border-color: var(--el-color-primary); }
dl { display: grid; grid-template-columns: 110px minmax(0, 1fr); gap: 10px; } dd { margin: 0; overflow-wrap: anywhere; } .model { border-bottom: 1px solid var(--el-border-color); }
textarea { display: block; box-sizing: border-box; width: 100%; margin: 8px 0; font: inherit; } button, textarea { padding: 8px; border: 1px solid var(--el-border-color); border-radius: 6px; background: var(--el-bg-color); color: var(--el-text-color-primary); }
button:focus-visible, textarea:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; } button:disabled { opacity: .5; } .actions { justify-content: flex-start; }
@media (max-width: 760px) { .reviews { padding: 14px; } .layout { grid-template-columns: 1fr; } button { min-height: 44px; } }
</style>
