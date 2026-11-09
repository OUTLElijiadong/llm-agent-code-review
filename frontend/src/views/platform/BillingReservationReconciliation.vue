<template>
  <main class="reconciliation-admin" :aria-busy="loading || busy">
    <header class="page-head">
      <div>
        <p class="eyebrow">平台财务工作台</p>
        <h1>模型与工具账本对账</h1>
        <p>核对模型 Token、Search 和 Fetch 的积分预占异常状态；支付宝和微信支付订单对账在“渠道账单对账”中。</p>
      </div>
      <button type="button" :disabled="loading || busy" @click="load">刷新记录</button>
    </header>

    <section class="boundary-note" aria-label="模型与工具账本对账边界">
      <strong>双人复核，按原快照计算</strong>
      <span>只有 <code>unknown</code> 可提交结算或释放提案。陈旧 Search/Fetch 的外呼标记超过 30 分钟时，可经核验转为 <code>unknown</code>；此操作只改变对账状态，不释放额度。其他 <code>reserved</code> 继续只读。</span>
      <span>每项提案须由另一位获授权的管理员复核。页面只保存外部证据引用和 SHA-256；平台不会验证外部证据的真实性。</span>
    </section>

    <BillingC4ReviewerManagement />

    <p v-if="error" class="message error" role="alert">{{ error }}</p>
    <p v-if="notice" class="message notice" role="status">{{ notice }}</p>

    <section class="panel filters" aria-label="模型预占筛选">
      <label class="field">
        <span>预占状态</span>
        <select v-model="status" :disabled="loading || busy" @change="load">
          <option value="">全部（含只读 reserved）</option>
          <option value="unknown">结果未知（可提案）</option>
          <option value="reserved">仍预占（只读）</option>
          <option value="reviewed">已有审计历史</option>
        </select>
      </label>
      <label class="field">
        <span>计费类别</span>
        <select v-model="billableKind" :disabled="loading || busy" @change="offset = 0; load()">
          <option value="">全部类型</option>
          <option value="model">模型 Token</option>
          <option value="search">Search 按次</option>
          <option value="fetch">Fetch 按次</option>
        </select>
      </label>
      <span class="count">{{ total }} 条记录</span>
    </section>

    <section v-if="loading && rows.length === 0" class="panel empty" role="status">正在读取未结模型和工具预占…</section>
    <section v-else-if="rows.length === 0" class="panel empty">当前筛选下没有未结模型或工具预占。</section>
    <section v-else class="panel table-wrap" aria-label="模型与工具预占记录">
      <table>
        <thead>
          <tr><th>记录</th><th>模型 / 工具与归属</th><th>状态</th><th>预占</th><th>快照</th><th>供应商用量</th><th>创建时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id" :class="{ selected: selected?.id === row.id }">
            <td><strong>#{{ row.id }}</strong><small>…{{ row.request_key_suffix }}</small></td>
            <td>{{ billableLabel(row) }} · {{ row.model_name }}<small>{{ ownerLabel(row) }}</small></td>
            <td><span :class="['status-pill', row.status]">{{ statusLabel(row.status) }}</span>
              <small v-if="row.pending_proposal">等待双人复核</small></td>
            <td>{{ credits(row.held_milli) }}<small>请求 {{ credits(row.requested_milli) }}</small></td>
            <td><small>供应商 {{ row.has_provider_snapshot ? '完整' : '缺失' }}</small>
              <small>费率 {{ row.has_rate_snapshot ? '完整' : '缺失' }}</small></td>
            <td v-if="row.billable_kind === 'model'">模型内 Search {{ row.server_tool_search_calls ?? '—' }}<small>Fetch {{ row.server_tool_fetch_calls ?? '—' }}</small></td>
            <td v-else>{{ row.tool_successful_calls === null ? '成功次数待确认' : `成功 ${row.tool_successful_calls}` }}<small>Attempt {{ row.provider_attempt_started_at ? '已开始' : '未开始' }}</small></td>
            <td>{{ dateTime(row.created_at) }}</td>
            <td><button type="button" :aria-pressed="selected?.id === row.id" @click="select(row)">{{ selected?.id === row.id ? '当前记录' : '查看' }}</button></td>
          </tr>
        </tbody>
      </table>
      <footer v-if="total > PAGE_SIZE" class="pagination">
        <span>第 {{ Math.floor(offset / PAGE_SIZE) + 1 }} / {{ Math.ceil(total / PAGE_SIZE) }} 页</span>
        <div>
          <button type="button" :disabled="offset === 0 || loading" @click="changePage(-1)">上一页</button>
          <button type="button" :disabled="offset + PAGE_SIZE >= total || loading" @click="changePage(1)">下一页</button>
        </div>
      </footer>
    </section>

    <section v-if="selected" class="panel detail" aria-live="polite" aria-labelledby="detail-title">
      <header class="detail-head">
        <div><h2 id="detail-title">预占 #{{ selected.id }}</h2><p>{{ billableLabel(selected) }} · {{ selected.model_name }} · …{{ selected.request_key_suffix }}</p></div>
        <span :class="['status-pill', selected.status]">{{ statusLabel(selected.status) }}</span>
      </header>
      <dl class="facts">
        <div><dt>付款归属</dt><dd>{{ ownerLabel(selected) }}</dd></div>
        <div><dt>调用类型</dt><dd>{{ selected.usage_kind }}</dd></div>
        <div><dt>原始预占</dt><dd>{{ credits(selected.requested_milli) }}</dd></div>
        <div><dt>当前冻结</dt><dd>{{ credits(selected.held_milli) }}</dd></div>
        <div><dt>费率快照</dt><dd>{{ selected.has_rate_snapshot ? selected.pricing_version : '缺失，禁止结算' }}</dd></div>
        <div><dt>供应商快照</dt><dd>{{ selected.has_provider_snapshot ? selected.provider_identity_version : '缺失，禁止结算' }}</dd></div>
        <div v-if="selected.billable_kind === 'model'"><dt>已报告 Token</dt><dd>输入 {{ tokenCount(selected.input_tokens) }} · 输出 {{ tokenCount(selected.output_tokens) }}</dd></div>
        <div v-else><dt>工具费率快照</dt><dd>供应商直采 {{ number(selected.tool_cost_cny_micros_per_success) }} 微元人民币/次；用户费率 {{ credits(selected.tool_price_milli_per_success) }}/次</dd></div>
        <div v-if="selected.billable_kind !== 'model'"><dt>外呼 Attempt</dt><dd>{{ selected.provider_attempt_started_at ? dateTime(selected.provider_attempt_started_at) : '尚未开始' }}</dd></div>
        <div><dt>创建时间</dt><dd>{{ dateTime(selected.created_at) }}</dd></div>
      </dl>

      <p v-if="selected.status === 'reserved'" class="hint warning">该调用仍可能在供应商处理中。平台只会把超过安全等待窗口且运行租约已失效的 Search/Fetch 转为 unknown；不会自动释放额度。</p>
      <div v-if="selected.can_recover_stale_attempt" class="recovery-action">
        <p class="hint warning">外呼标记已超过 30 分钟。确认运行租约已失效后，可将该预占转入双人对账；不会扣除或释放额度。</p>
        <button type="button" class="primary" :disabled="busy" @click="recoverStaleAttempt">转入人工对账</button>
      </div>
      <p v-if="selected.status === 'unknown' && !selected.can_settle" class="hint warning" role="status">
        当前不能结算：{{ selected.settlement_block_reasons.join('；') }}。缺少用量字段时按未知处理，不推断为零。
      </p>

      <section v-if="history.length" class="history" aria-label="对账审计历史">
        <h3>审计历史</h3>
        <ol>
          <li v-for="event in history" :key="event.id">
            <div class="event-head"><strong>{{ eventLabel(event) }}</strong><time>{{ dateTime(event.created_at) }}</time></div>
            <p>操作人 #{{ event.actor_id }} · {{ resolutionLabel(event.resolution) }}<span v-if="event.actual_milli !== null"> · 重新计算 {{ credits(event.actual_milli) }}</span><span v-if="selected.billable_kind !== 'model' && event.tool_successful_calls !== null"> · 工具成功次数 {{ event.tool_successful_calls }}</span></p>
            <p v-if="event.provider_request_id">供应商请求 ID：<code>{{ event.provider_request_id }}</code></p>
            <p v-if="event.evidence_reference">证据引用：{{ event.evidence_reference }} · SHA-256 <code>{{ event.evidence_sha256 }}</code></p>
            <p v-else-if="event.decision === 'blocked'">此业务阻断事件不含用户提交的证据字段。</p>
            <p>{{ event.note }}</p>
          </li>
        </ol>
      </section>
      <p v-else class="muted" role="status">该预占尚无人工对账事件。</p>

      <section v-if="pendingProposal" class="proposal-review" aria-label="待审批提案">
        <h3>待另一位获授权管理员复核</h3>
        <p class="hint">{{ resolutionLabel(pendingProposal.resolution) }}<span v-if="pendingProposal.actual_milli !== null">；按原费率快照计算为 {{ credits(pendingProposal.actual_milli) }}</span></p>
        <p>提案人 #{{ pendingProposal.actor_id }} · 证据引用 {{ pendingProposal.evidence_reference }}</p>
        <p v-if="pendingProposal.provider_request_id">供应商请求 ID：<code>{{ pendingProposal.provider_request_id }}</code></p>
        <p v-if="selected.billable_kind === 'model'">Token：输入 {{ tokenCount(pendingProposal.input_tokens) }} · 输出 {{ tokenCount(pendingProposal.output_tokens) }}</p>
        <p v-else>工具成功次数：{{ pendingProposal.tool_successful_calls ?? '未记录' }}</p>
        <p class="evidence-note">请在供应商控制台/外部证据源独立核验后再决定。证据 SHA-256 只能标识提交内容，不能证明其真实性。</p>
        <template v-if="pendingProposal.actor_id === currentUserId">
          <p class="hint warning" role="status">你是提案人，不能审批本人提案；需另一位获授权管理员复核。</p>
        </template>
        <template v-else>
          <label class="field decision-note"><span>复核说明</span><textarea v-model.trim="decisionNote" maxlength="500" rows="3" placeholder="写明证据核验方式和审批依据" /></label>
          <div class="actions">
            <button type="button" class="danger" :disabled="busy || !decisionNote.trim()" @click="decide('reject')">拒绝提案</button>
            <button type="button" class="primary" :disabled="busy || !decisionNote.trim()" @click="decide('approve')">{{ busy ? '提交中…' : '批准并执行账本变更' }}</button>
          </div>
          <p class="hint warning">批准会在同一事务中写入审批事件并结算或释放额度，完成后不能从此页面撤销。</p>
        </template>
      </section>

      <section v-else-if="selected.status === 'unknown'" class="proposal-form" aria-label="提交对账提案">
        <h3>提交人工提案</h3>
        <label class="field"><span>处理方式</span>
          <select v-model="resolution" :disabled="busy">
            <option value="settle" :disabled="!selected.can_settle">按原费率快照结算</option>
            <option value="release_not_sent" :disabled="!selected.can_release_not_sent">证据证明请求从未发送，释放预占</option>
          </select>
        </label>
        <p v-if="resolution === 'settle' && selected.settlement_block_reasons.length" class="hint warning" role="status">
          当前不能提交结算：{{ selected.settlement_block_reasons.join('；') }}。
        </p>
        <p v-if="resolution === 'release_not_sent' && selected.release_block_reasons.length" class="hint warning" role="status">
          当前不能提交释放：{{ selected.release_block_reasons.join('；') }}。
        </p>
        <template v-if="resolution === 'settle'">
          <label class="field"><span>供应商请求 ID</span><input v-model.trim="providerRequestId" maxlength="128" autocomplete="off" placeholder="来自供应商响应或账单的请求 ID" /></label>
          <div v-if="selected.billable_kind === 'model'" class="token-inputs">
            <label class="field"><span>供应商确认输入 Token</span><input v-model.number="inputTokens" type="number" min="0" step="1" inputmode="numeric" /></label>
            <label class="field"><span>供应商确认输出 Token</span><input v-model.number="outputTokens" type="number" min="0" step="1" inputmode="numeric" /></label>
          </div>
          <label v-else class="field"><span>外部证据确认的成功请求次数（本次最多 1 次）</span>
            <select v-model.number="toolSuccessfulCalls" :disabled="busy">
              <option :value="null">选择 0 或 1</option><option :value="0">0 次：有证据证明没有可计费成功请求</option>
              <option :value="1">1 次：供应商记录证明成功请求</option>
            </select>
          </label>
          <p class="hint" v-if="selected.billable_kind === 'model'">批准金额由原始费率快照计算。若存在 Search/Fetch 成功调用，系统不会结算。</p>
          <p class="hint" v-else>金额按本次 reservation 保存的单次费率重算。0 次是有证据支持的零收费对账，不等同于“请求从未发送”。</p>
        </template>
        <template v-else>
          <p class="hint warning">释放会归还仍冻结的积分与企业额度。只有外部证据明确证明供应商从未收到请求时才可提案；已记录任何供应商用量的预占不能释放。</p>
        </template>
        <label class="field"><span>外部证据引用</span><input v-model.trim="evidenceReference" maxlength="240" autocomplete="off" placeholder="供应商工单号、账单行号或内部证据编号" /></label>
        <label class="field"><span>证据 SHA-256</span><input v-model.trim="evidenceSha256" maxlength="64" autocomplete="off" spellcheck="false" placeholder="64 位十六进制摘要" /></label>
        <label class="field"><span>提案说明</span><textarea v-model.trim="proposalNote" maxlength="500" rows="3" placeholder="说明外部证据来源和核对结论；不要粘贴密钥、提示词或对话正文" /></label>
        <button class="primary" type="button" :disabled="!canPropose" @click="propose">{{ busy ? '提交中…' : '提交给另一位获授权管理员复核' }}</button>
      </section>
    </section>
  </main>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useUserStore } from '@/stores/user'
import { getToken } from '@/utils/token'
import BillingC4ReviewerManagement from './BillingC4ReviewerManagement.vue'
import {
  decideBillingReservationReconciliation,
  listBillingReservationReconciliation,
  listBillingReservationReconciliationEvents,
  proposeBillingReservationReconciliation,
  recoverStaleBillingReservationAttempt,
  type BillingReservationReconciliation,
  type BillingReservationReconciliationEvent,
} from '@/api/billing'

const PAGE_SIZE = 100
const status = ref<'' | 'reserved' | 'unknown' | 'reviewed'>('')
const billableKind = ref<'' | 'model' | 'search' | 'fetch'>('')
const rows = ref<BillingReservationReconciliation[]>([])
const selected = ref<BillingReservationReconciliation | null>(null)
const history = ref<BillingReservationReconciliationEvent[]>([])
const total = ref(0)
const offset = ref(0)
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const resolution = ref<'settle' | 'release_not_sent'>('settle')
const providerRequestId = ref('')
const inputTokens = ref<number | null>(null)
const outputTokens = ref<number | null>(null)
const toolSuccessfulCalls = ref<0 | 1 | null>(null)
const evidenceReference = ref('')
const evidenceSha256 = ref('')
const proposalNote = ref('')
const decisionNote = ref('')
const user = useUserStore()
const currentUserId = computed(() => Number(user.profile?.id ?? 0))
let generation = 0

const pendingProposal = computed(() => selected.value?.pending_proposal ?? null)
const canPropose = computed(() => {
  const row = selected.value
  if (!row || row.status !== 'unknown' || pendingProposal.value || busy.value) return false
  if (!evidenceReference.value.trim() || !/^[\da-f]{64}$/i.test(evidenceSha256.value.trim()) || !proposalNote.value.trim()) return false
  if (resolution.value === 'release_not_sent') return row.can_release_not_sent
  if (!row.can_settle || !providerRequestId.value.trim()) return false
  if (row.billable_kind !== 'model') return toolSuccessfulCalls.value === 0 || toolSuccessfulCalls.value === 1
  return Number.isSafeInteger(inputTokens.value) && Number(inputTokens.value) >= 0
    && Number.isSafeInteger(outputTokens.value) && Number(outputTokens.value) >= 0
})

function tokenSnapshot() { return getToken() }
function ownerLabel(row: BillingReservationReconciliation) {
  if (row.enterprise_id !== null) return `企业 #${row.enterprise_id}${row.group_id ? ` · 小组 #${row.group_id}` : ''} · 发起人 #${row.user_id ?? '系统'}`
  return row.user_id ? `个人用户 #${row.user_id}` : '无用户归属'
}
function billableLabel(row: BillingReservationReconciliation) {
  if (row.billable_kind === 'search') return 'Search 按次'
  if (row.billable_kind === 'fetch') return 'Fetch 按次'
  return '模型 Token'
}
function credits(amount: number | null) { return amount === null ? '—' : `${(amount / 1000).toLocaleString('zh-CN', { maximumFractionDigits: 3 })} 积分` }
function number(value: number | null) { return value === null ? '缺失' : value.toLocaleString('zh-CN') }
function tokenCount(value: number | null) { return value === null ? '未记录' : value.toLocaleString('zh-CN') }
function statusLabel(value: BillingReservationReconciliation['status']) {
  return ({ unknown: '结果未知', reserved: '仍预占', settled: '已结算', released: '已释放' })[value]
}
function dateTime(value: string) { return new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) }
function resolutionLabel(value: BillingReservationReconciliationEvent['resolution']) {
  if (value === null) return '未关联账本动作'
  return value === 'settle' ? '按原费率快照结算' : '证据证明未发送，释放预占'
}
function eventLabel(event: BillingReservationReconciliationEvent) {
  if (event.decision === 'proposed') return '提交提案'
  if (event.decision === 'approved') return '另一位管理员批准'
  if (event.decision === 'blocked') return '操作被阻断，账本未变更'
  if (event.decision === 'recovered') return '陈旧预占转入 unknown'
  return '另一位管理员拒绝'
}
function clearForm() {
  providerRequestId.value = ''; inputTokens.value = null; outputTokens.value = null; toolSuccessfulCalls.value = null
  evidenceReference.value = ''; evidenceSha256.value = ''; proposalNote.value = ''; decisionNote.value = ''
}
function resetSelection() { selected.value = null; history.value = []; clearForm() }

async function load() {
  const current = ++generation, account = currentUserId.value, token = tokenSnapshot()
  if (!account) return
  loading.value = true; error.value = ''; notice.value = ''
  try {
    const result = await listBillingReservationReconciliation(status.value || undefined, token, {
      limit: PAGE_SIZE, offset: offset.value,
      ...(billableKind.value ? { billable_kind: billableKind.value } : {}),
    })
    if (current !== generation || account !== currentUserId.value || token !== tokenSnapshot()) return
    rows.value = result.items; total.value = result.total
    if (selected.value) {
      const currentRow = result.items.find(row => row.id === selected.value?.id)
      if (currentRow) {
        selected.value = currentRow
        await loadHistory(current, account, token, currentRow.id)
      } else resetSelection()
    }
  } catch (reason) {
    if (current === generation && account === currentUserId.value && token === tokenSnapshot()) {
      error.value = reason instanceof Error ? reason.message : '模型账本对账记录加载失败。'
    }
  } finally {
    if (current === generation && account === currentUserId.value && token === tokenSnapshot()) loading.value = false
  }
}
async function loadHistory(current: number, account: number, token: string | null, reservationId: number) {
  const result = await listBillingReservationReconciliationEvents(reservationId, token)
  if (current !== generation || account !== currentUserId.value || token !== tokenSnapshot()
      || selected.value?.id !== reservationId) return
  history.value = result.items
}
async function select(row: BillingReservationReconciliation) {
  const current = ++generation, account = currentUserId.value, token = tokenSnapshot()
  selected.value = row; history.value = []; clearForm()
  resolution.value = row.can_settle ? 'settle' : 'release_not_sent'
  loading.value = true; error.value = ''
  try { await loadHistory(current, account, token, row.id) }
  catch (reason) {
    if (current === generation && selected.value?.id === row.id) {
      error.value = reason instanceof Error ? reason.message : '对账审计历史加载失败。'
    }
  } finally { if (current === generation) loading.value = false }
}
async function runAction(action: () => Promise<unknown>, success: string) {
  const current = generation, account = currentUserId.value, token = tokenSnapshot()
  if (!selected.value || busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    await action()
    if (current !== generation || account !== currentUserId.value || token !== tokenSnapshot()) return
    await load()
    if (account === currentUserId.value && token === tokenSnapshot()) notice.value = success
  } catch (reason) {
    if (current === generation && account === currentUserId.value && token === tokenSnapshot()) {
      const message = reason instanceof Error ? reason.message : '对账操作失败；请刷新并检查当前账本状态。'
      await load()
      if (account === currentUserId.value && token === tokenSnapshot()) error.value = message
    }
  } finally { busy.value = false }
}
function propose() {
  const row = selected.value
  if (!row || !canPropose.value) return
  const input = {
    resolution: resolution.value,
    ...(resolution.value === 'settle' ? {
      provider_request_id: providerRequestId.value.trim(),
      ...(row.billable_kind === 'model'
        ? { input_tokens: Number(inputTokens.value), output_tokens: Number(outputTokens.value) }
        : { tool_successful_calls: toolSuccessfulCalls.value as 0 | 1 }),
    } : {}),
    evidence_reference: evidenceReference.value.trim(), evidence_sha256: evidenceSha256.value.trim(),
    note: proposalNote.value.trim(),
  }
  return runAction(() => proposeBillingReservationReconciliation(row.id, input, tokenSnapshot()),
    '提案已提交。必须由另一位获授权管理员复核后才会改动账本。')
}
function decide(decision: 'approve' | 'reject') {
  const row = selected.value, note = decisionNote.value.trim()
  if (!row || !pendingProposal.value || !note) return
  const success = decision === 'approve'
    ? '复核通过，账本变更已完成。'
    : row.status === 'unknown'
      ? '提案已拒绝，预占继续冻结。'
      : '过期提案已拒绝并归档；账本此前已终结，没有再次变更。'
  return runAction(() => decideBillingReservationReconciliation(row.id, decision, note, tokenSnapshot()),
    success)
}
function recoverStaleAttempt() {
  const row = selected.value
  if (!row?.can_recover_stale_attempt || busy.value) return
  const confirmed = window.confirm(
    '确认该企业小菱运行租约已失效，并将陈旧 Search/Fetch 预占转为 unknown？额度不会释放，后续仍须双人对账。',
  )
  if (!confirmed) return
  return runAction(
    () => recoverStaleBillingReservationAttempt(
      row.id,
      '平台管理员确认外呼标记超过 30 分钟且运行租约已失效；仅转入双人对账，不自动释放额度。',
      tokenSnapshot(),
    ),
    '预占已转为 unknown，额度仍冻结；请按外部证据提交双人复核提案。',
  )
}
function changePage(direction: -1 | 1) {
  offset.value = Math.max(0, Math.min(offset.value + direction * PAGE_SIZE, Math.max(0, total.value - PAGE_SIZE)))
  resetSelection(); void load()
}
onMounted(() => { void load() })
watch([currentUserId, () => user.token], () => {
  generation++; rows.value = []; total.value = 0; offset.value = 0; resetSelection()
  loading.value = false; busy.value = false; error.value = ''; notice.value = ''
  if (currentUserId.value) void load()
}, { flush: 'sync' })
onBeforeUnmount(() => { generation++ })
</script>

<style scoped>
.reconciliation-admin { max-width: 1540px; margin: 0 auto; padding: 24px; color: #20243a; }
.page-head, .detail-head, .event-head, .actions, .pagination { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.page-head { margin-bottom: 18px; }.eyebrow { margin: 0 0 5px; color: #5667b3; font-size: 12px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
h1, h2, h3, p { margin-top: 0; }.page-head p:not(.eyebrow), .muted, .hint, .evidence-note { color: #687187; font-size: 13px; line-height: 1.6; }
.panel { margin-bottom: 16px; padding: 16px; border: 1px solid #e7e9f1; border-radius: 12px; background: #fff; box-shadow: 0 4px 14px rgb(24 32 56 / 4%); }
.boundary-note { display: grid; gap: 6px; margin-bottom: 16px; padding: 13px 15px; border: 1px solid #f1d9a8; border-radius: 10px; color: #624c22; background: #fffaf0; font-size: 13px; line-height: 1.55; }
button { min-height: 34px; padding: 7px 12px; border: 1px solid #d8dce6; border-radius: 8px; color: #30364b; background: #fff; font: inherit; cursor: pointer; }
button:disabled { opacity: .55; cursor: not-allowed; }.primary { border-color: #4f70cb; color: #fff; background: #4f70cb; }.danger { border-color: #d44b55; color: #bd3340; }
.filters { display: flex; align-items: end; justify-content: space-between; }.field { display: grid; gap: 6px; color: #434a5d; font-size: 13px; }
select, input, textarea { box-sizing: border-box; width: 100%; min-height: 36px; padding: 8px 10px; border: 1px solid #d8dce6; border-radius: 8px; color: inherit; background: #fff; font: inherit; }
textarea { resize: vertical; }.count { color: #6b7283; font-size: 13px; }.empty { color: #69718a; text-align: center; }
.table-wrap { overflow-x: auto; padding: 0; }table { width: 100%; border-collapse: collapse; font-size: 12px; white-space: nowrap; }
th, td { padding: 10px 11px; border-bottom: 1px solid #eceef3; text-align: left; vertical-align: top; }th { color: #71798b; background: #fafbfc; font-weight: 600; }
td small { display: block; margin-top: 4px; color: #7b8292; }.selected { background: #f1f3ff; }.status-pill { display: inline-block; padding: 4px 8px; border-radius: 20px; color: #765117; background: #fff3d8; font-size: 11px; font-weight: 650; }
.status-pill.unknown { color: #8a321f; background: #fff0e8; }.status-pill.reserved { color: #505a78; background: #eef1f8; }
.detail h2 { margin-bottom: 3px; font-size: 18px; }.detail-head p { margin-bottom: 0; color: #69718a; font-size: 12px; }.facts { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 10px 24px; margin: 20px 0; }
.facts div { display: grid; grid-template-columns: 105px minmax(0,1fr); gap: 8px; }dt { color: #69718a; font-size: 13px; }dd { margin: 0; overflow-wrap: anywhere; font-size: 13px; }
.history, .proposal-review, .proposal-form { margin-top: 18px; padding-top: 16px; border-top: 1px solid #eceef3; }.history h3, .proposal-review h3, .proposal-form h3 { margin-bottom: 10px; font-size: 15px; }
.history ol { display: grid; gap: 12px; margin: 0; padding-left: 22px; }.history li { padding-left: 4px; }.event-head { justify-content: flex-start; }.event-head time { color: #7b8292; font-size: 11px; }.history li p, .proposal-review p { margin: 5px 0; color: #596174; font-size: 12px; overflow-wrap: anywhere; }
.proposal-form { display: grid; gap: 12px; max-width: 760px; }.token-inputs { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 12px; }.decision-note { max-width: 760px; }.warning { padding: 10px 12px; border-radius: 8px; color: #754b14; background: #fff6e3; }.evidence-note { padding: 9px 11px; border-radius: 8px; background: #f5f6fa; }.actions { justify-content: flex-start; margin-top: 12px; }.pagination { padding: 12px 15px; color: #69718a; font-size: 12px; }.pagination div { display: flex; gap: 8px; }
.message { padding: 12px 14px; border-radius: 9px; }.error { color: #aa2732; background: #fff1f1; }.notice { color: #256344; background: #edf8f1; }
@media (max-width: 720px) { .reconciliation-admin { padding: 15px; }.page-head, .detail-head, .filters { align-items: flex-start; flex-direction: column; }.facts, .token-inputs { grid-template-columns: 1fr; }.facts div { grid-template-columns: 95px minmax(0,1fr); }.actions { flex-wrap: wrap; } }
</style>
