<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useUserStore } from '@/stores/user'
import { getToken } from '@/utils/token'
import { changeBillingC4Reviewer, listBillingC4Reviewers, changeBillingRateReviewer, listBillingRateReviewers,
  changeBillingRefundReviewer, listBillingRefundReviewers, type BillingC4Reviewer } from '@/api/billing'
import { getUsers } from '@/api/user'
import type { UserListItem } from '@/types/user'

const props = withDefaults(defineProps<{ kind?: 'c4' | 'rate' | 'refund' }>(), { kind: 'c4' })
const user = useUserStore()
const userId = computed(() => Number(user.profile?.id || 0))
const open = ref(false)
const reviewers = ref<BillingC4Reviewer[]>([])
const candidates = ref<UserListItem[]>([])
const selected = ref<UserListItem | null>(null)
const keyword = ref('')
const reason = ref('')
const total = ref(0)
const offset = ref(0)
const loading = ref(false)
const searching = ref(false)
const busy = ref(false)
const error = ref('')
const message = ref('')
let scope = 0
let listRequest = 0
let searchRequest = 0

function current(generation: number, account: number, token: string | null): boolean {
  return generation === scope && account === userId.value && token === getToken()
}
function isForbidden(cause: unknown): boolean {
  const code = cause && typeof cause === 'object' && 'code' in cause ? Number(cause.code) : 0
  return code === 403 || (code >= 40300 && code < 40400)
}
function fail(cause: unknown, fallback: string): void {
  if (isForbidden(cause)) {
    reviewers.value = []; candidates.value = []; selected.value = null; total.value = 0; reason.value = ''
    error.value = '当前账号无权管理复核人。'
  } else error.value = fallback
}
function clear(): void {
  open.value = false; reviewers.value = []; candidates.value = []; selected.value = null
  keyword.value = ''; reason.value = ''; total.value = 0; offset.value = 0
  loading.value = false; searching.value = false; busy.value = false; error.value = ''; message.value = ''
}
async function load(): Promise<void> {
  const generation = scope, request = ++listRequest, account = userId.value, token = getToken()
  if (!account || !token) return
  loading.value = true
  try {
    const list = props.kind === 'rate' ? listBillingRateReviewers : props.kind === 'refund' ? listBillingRefundReviewers : listBillingC4Reviewers
    const result = await list(50, offset.value, token)
    if (!current(generation, account, token) || request !== listRequest) return
    reviewers.value = result.items; total.value = result.total
  } catch (cause) {
    if (current(generation, account, token) && request === listRequest) fail(cause, '复核人列表加载失败。')
  } finally { if (current(generation, account, token) && request === listRequest) loading.value = false }
}
function toggle(): void {
  open.value = !open.value
  if (open.value) { error.value = ''; void load() }
}
async function search(): Promise<void> {
  const term = keyword.value.trim(), generation = scope, request = ++searchRequest, account = userId.value, token = getToken()
  if (!term || !account || !token) { candidates.value = []; error.value = '请输入管理员用户名。'; return }
  searching.value = true; error.value = ''; message.value = ''; selected.value = null
  try {
    const result = await getUsers({ keyword: term, role: 'admin', status: '1', page: 1, page_size: 20 }, token)
    if (!current(generation, account, token) || request !== searchRequest) return
    candidates.value = result.items
  } catch (cause) {
    if (current(generation, account, token) && request === searchRequest) fail(cause, '管理员搜索失败。')
  } finally { if (current(generation, account, token) && request === searchRequest) searching.value = false }
}
async function change(id: number, active: boolean): Promise<void> {
  const explanation = reason.value.trim(), generation = scope, account = userId.value, token = getToken()
  if (!explanation || !account || !token || busy.value) return
  busy.value = true; error.value = ''; message.value = ''
  try {
    const update = props.kind === 'rate' ? changeBillingRateReviewer : props.kind === 'refund' ? changeBillingRefundReviewer : changeBillingC4Reviewer
    await update(id, active, explanation, token)
    if (!current(generation, account, token)) return
    reason.value = ''; message.value = active ? '已授权。' : '已撤销。'
    await load()
  } catch (cause) {
    if (current(generation, account, token)) fail(cause, '操作未完成，请刷新并核对账号状态。')
  } finally { if (current(generation, account, token)) busy.value = false }
}
function page(step: -1 | 1): void {
  offset.value = Math.max(0, offset.value + step * 50); void load()
}
watch([userId, () => user.token], () => { ++scope; ++listRequest; ++searchRequest; clear() }, { flush: 'sync' })
onBeforeUnmount(() => { ++scope; ++listRequest; ++searchRequest })
</script>

<template>
  <section class="reviewers" aria-label="复核人委任">
    <button type="button" data-action="toggle" :aria-expanded="open" @click="toggle">{{ open ? '收起复核人' : '管理复核人' }}</button>
    <div v-if="open" class="content">
      <p>{{ kind === 'rate' ? '复核权仅用于费率审批。' : kind === 'refund' ? '审核权仅用于会员和席位退款。' : '复核权仅用于账本提案。' }}</p>
      <p v-if="error" role="alert" class="error">{{ error }}</p>
      <p v-if="message" role="status">{{ message }}</p>
      <label :for="`${kind}-grant-reason`">授权或撤销原因</label>
      <input :id="`${kind}-grant-reason`" v-model="reason" maxlength="500" :disabled="busy" />
      <div class="search">
        <label :for="`${kind}-user-search`">搜索平台管理员</label>
        <input :id="`${kind}-user-search`" v-model="keyword" :disabled="busy || searching" @keydown.enter.prevent="search" />
        <button type="button" data-action="search" :disabled="busy || searching || !keyword.trim()" @click="search">搜索</button>
      </div>
      <div v-if="candidates.length" aria-label="搜索结果">
        <button v-for="item in candidates" :key="item.id" type="button" :data-user="item.id"
                :aria-pressed="selected?.id === item.id" :disabled="busy" @click="selected = item">
          {{ item.nickname && item.nickname !== item.username ? `${item.nickname}（${item.username}）` : item.username }}
        </button>
      </div>
      <button v-if="selected" type="button" data-action="grant" :disabled="busy || !reason.trim()"
              @click="change(selected.id, true)">授权 {{ selected.username }}</button>
      <h3>复核人记录</h3>
      <p v-if="loading" role="status">正在加载…</p>
      <p v-else-if="!reviewers.length">暂无记录。</p>
      <ul v-else>
        <li v-for="row in reviewers" :key="row.user_id">
          <span>{{ row.nickname || row.username || `#${row.user_id}` }}<template v-if="row.nickname && row.username">（{{ row.username }}）</template> · {{ !row.active ? '已撤销' : row.eligible ? '可复核' : '已授权，账号当前不可用' }}</span>
          <button v-if="row.active" type="button" :data-revoke="row.user_id" :disabled="busy || !reason.trim()"
                  @click="change(row.user_id, false)">撤销</button>
        </li>
      </ul>
      <div class="pager">
        <button v-if="offset" type="button" :disabled="loading || busy" @click="page(-1)">上一页</button>
        <button v-if="offset + 50 < total" type="button" :disabled="loading || busy" @click="page(1)">下一页</button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.reviewers { margin-bottom: 16px; } .content { margin-top: 12px; padding: 14px; border: 1px solid var(--el-border-color); border-radius: 8px; }
label { display: block; margin: 8px 0; } input { box-sizing: border-box; width: 100%; max-width: 420px; }
.search { margin: 10px 0; } button, input { font: inherit; padding: 8px; border: 1px solid var(--el-border-color); border-radius: 6px; }
button { margin-right: 8px; } button[aria-pressed="true"] { border-color: var(--el-color-primary); }
button:focus-visible, input:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }
button:disabled { opacity: .5; } li { display: flex; align-items: center; gap: 12px; margin: 8px 0; } .error { color: var(--el-color-danger); }
@media (max-width: 760px) { button { min-height: 44px; } }
</style>
