<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useUserStore } from '@/stores/user'
import { getToken } from '@/utils/token'
import { decideBillingReservationReconciliation, getBillingC4Review, listBillingC4Reviews,
  type BillingC4Review } from '@/api/billing'

const user = useUserStore()
const userId = computed(() => Number(user.profile?.id || 0))
const rows = ref<BillingC4Review[]>([])
const selected = ref<BillingC4Review | null>(null)
const note = ref('')
const loading = ref(false)
const busy = ref(false)
const denied = ref(false)
const error = ref('')
const message = ref('')
const offset = ref(0)
const hasMore = ref(false)
const PAGE_SIZE = 50
let generation = 0

function current(sequence: number, account: number, token: string | null): boolean {
  return sequence === generation && account === userId.value && token === getToken()
}
function isForbidden(cause: unknown): boolean {
  const code = cause && typeof cause === 'object' && 'code' in cause ? Number(cause.code) : 0
  return code === 403 || (code >= 40300 && code < 40400)
}
function clear(): void {
  rows.value = []; selected.value = null; note.value = ''; offset.value = 0; hasMore.value = false
  loading.value = false; busy.value = false; denied.value = false; error.value = ''; message.value = ''
}
function fail(cause: unknown, fallback: string): void {
  if (isForbidden(cause)) {
    rows.value = []; selected.value = null; note.value = ''; denied.value = true; hasMore.value = false
    error.value = '当前账号尚无复核权限，请联系平台最高管理员。'
  } else error.value = fallback
}
async function load(): Promise<void> {
  const sequence = ++generation, account = userId.value, token = getToken()
  if (!account || !token) return
  loading.value = true; error.value = ''; denied.value = false
  try {
    const page = await listBillingC4Reviews(PAGE_SIZE, offset.value, token)
    if (!current(sequence, account, token)) return
    rows.value = page.items; hasMore.value = page.items.length === PAGE_SIZE
    selected.value = null; note.value = ''
  } catch (cause) {
    if (current(sequence, account, token)) fail(cause, '待复核列表加载失败，请重试。')
  } finally { if (current(sequence, account, token)) loading.value = false }
}
async function open(id: number): Promise<void> {
  const sequence = ++generation, account = userId.value, token = getToken()
  if (!account || !token || busy.value) return
  selected.value = null; note.value = ''; error.value = ''; message.value = ''; loading.value = true
  try {
    const result = await getBillingC4Review(id, token)
    if (current(sequence, account, token)) selected.value = result
  } catch (cause) {
    if (current(sequence, account, token)) fail(cause, '该提案已处理或无法读取，请刷新列表。')
  } finally { if (current(sequence, account, token)) loading.value = false }
}
async function decide(decision: 'approve' | 'reject'): Promise<void> {
  const row = selected.value, reason = note.value.trim(), account = userId.value, token = getToken()
  if (!row || !reason || busy.value || row.proposal.actor_id === account) return
  if (decision === 'approve' && !window.confirm('批准后将按提案执行账本变更，确定继续吗？')) return
  const sequence = generation
  busy.value = true; error.value = ''; message.value = ''
  try {
    await decideBillingReservationReconciliation(row.reservation.id, decision, reason, token)
    if (!current(sequence, account, token)) return
    await load()
    if (account === userId.value && token === getToken()) message.value = decision === 'approve' ? '复核已批准。' : '提案已拒绝。'
  } catch (cause) {
    if (current(sequence, account, token)) fail(cause, '复核未完成，请刷新并核对当前状态。')
  } finally { if (account === userId.value && token === getToken()) busy.value = false }
}
function page(step: -1 | 1): void {
  offset.value = Math.max(0, offset.value + step * PAGE_SIZE)
  void load()
}
onMounted(() => { void load() })
watch([userId, () => user.token], () => { ++generation; clear(); if (userId.value) void load() }, { flush: 'sync' })
onBeforeUnmount(() => { ++generation })
</script>

<template>
  <main class="reviews" :aria-busy="loading || busy">
    <header><h1>账本待复核</h1><button type="button" :disabled="loading || busy" @click="load">刷新</button></header>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="message" role="status">{{ message }}</p>
    <p v-if="loading && !rows.length" role="status">正在加载…</p>
    <p v-else-if="!rows.length && !denied">暂无待复核提案。</p>
    <div v-if="!denied && (rows.length || offset)" class="layout">
      <section aria-label="待复核提案">
        <button v-for="row in rows" :key="row.reservation.id" type="button" class="review-row"
                :data-review="row.reservation.id" :disabled="loading || busy"
                :aria-pressed="selected?.reservation.id === row.reservation.id" @click="open(row.reservation.id)">
          #{{ row.reservation.id }} · {{ row.reservation.model_name }}
        </button>
        <div class="actions">
          <button v-if="offset" type="button" :disabled="loading || busy" @click="page(-1)">上一页</button>
          <button v-if="hasMore" type="button" :disabled="loading || busy" @click="page(1)">下一页</button>
        </div>
      </section>
      <section v-if="selected" aria-label="提案详情">
        <h2>提案 #{{ selected.reservation.id }}</h2>
        <dl>
          <dt>处理</dt><dd>{{ selected.proposal.resolution === 'settle' ? '按原快照结算' : '证明未发送，释放预占' }}</dd>
          <dt>提案人</dt><dd>#{{ selected.proposal.actor_id }}</dd>
          <dt>预占积分</dt><dd>{{ (selected.reservation.requested_milli / 1000).toLocaleString('zh-CN') }}</dd>
          <dt>拟结算积分</dt><dd>{{ selected.proposal.actual_milli === null ? '—' : (selected.proposal.actual_milli / 1000).toLocaleString('zh-CN') }}</dd>
          <dt>供应商请求 ID</dt><dd>{{ selected.proposal.provider_request_id || '—' }}</dd>
          <dt v-if="selected.reservation.billable_kind === 'model'">Token</dt>
          <dd v-if="selected.reservation.billable_kind === 'model'">输入 {{ selected.proposal.input_tokens ?? '—' }} · 输出 {{ selected.proposal.output_tokens ?? '—' }}</dd>
          <dt v-else>成功次数</dt><dd v-if="selected.reservation.billable_kind !== 'model'">{{ selected.proposal.tool_successful_calls ?? '—' }}</dd>
          <dt>证据引用</dt><dd>{{ selected.proposal.evidence_reference || '—' }}</dd>
          <dt>证据摘要</dt><dd class="hash">{{ selected.proposal.evidence_sha256 || '—' }}</dd>
          <dt>提案说明</dt><dd>{{ selected.proposal.note }}</dd>
        </dl>
        <p>请独立核验外部证据后决定。</p>
        <p v-if="selected.proposal.actor_id === userId" role="status">不能复核自己的提案。</p>
        <template v-else>
          <label for="c4-review-note">复核说明</label>
          <textarea id="c4-review-note" v-model="note" maxlength="500" rows="3" :disabled="busy" />
          <div class="actions">
            <button type="button" :disabled="busy || !note.trim()" @click="decide('reject')">拒绝</button>
            <button type="button" data-action="approve" :disabled="busy || !note.trim()" @click="decide('approve')">批准并执行</button>
          </div>
        </template>
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
.review-row[aria-pressed="true"] { border-color: var(--el-color-primary); }
dl { display: grid; grid-template-columns: 110px minmax(0, 1fr); gap: 10px; } dd { margin: 0; overflow-wrap: anywhere; }
textarea { display: block; box-sizing: border-box; width: 100%; margin: 8px 0; font: inherit; }
button, textarea { padding: 8px; border: 1px solid var(--el-border-color); border-radius: 6px; background: var(--el-bg-color); color: var(--el-text-color-primary); }
button:focus-visible, textarea:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }
button:disabled { opacity: .5; } .actions { justify-content: flex-start; }
@media (max-width: 760px) { .reviews { padding: 14px; } .layout { grid-template-columns: 1fr; } button { min-height: 44px; } }
</style>
