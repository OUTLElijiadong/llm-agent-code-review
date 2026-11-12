<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { getToken } from '@/utils/token'
import { useUserStore } from '@/stores/user'
import { decidePersonalPublication, listPersonalPublicationReviews, revokePersonalPublication,
  type PersonalPublicationReview, type PublicationStatus } from '@/api/personalPublications'

const options: Array<{ value: PublicationStatus; label: string }> = [
  { value: 'pending', label: '待审核' }, { value: 'approved', label: '已发布' },
  { value: 'rejected', label: '已拒绝' }, { value: 'withdrawn', label: '已撤回' },
  { value: 'revoked', label: '已下架' },
]
const status = ref<PublicationStatus>('pending'), rows = ref<PersonalPublicationReview[]>([])
const user = useUserStore()
const loading = ref(false), busy = ref(false), more = ref(false), reason = ref(''), error = ref(''), notice = ref('')
let generation = 0, alive = true
async function load(append = false) {
  const seq = ++generation, token = getToken(), selected = status.value
  if (!token || token !== user.token || !user.profile?.id) return
  loading.value = true; error.value = ''
  try {
    const page = await listPersonalPublicationReviews(selected, token, append ? rows.value.length : 0)
    if (!alive || seq !== generation || token !== getToken() || selected !== status.value) return
    rows.value = append ? [...rows.value, ...page] : page; more.value = page.length === 100
  } catch {
    if (alive && seq === generation && token === getToken()) { rows.value = []; error.value = '审核记录加载失败，请重试。' }
  } finally { if (alive && seq === generation) loading.value = false }
}
async function decide(row: PersonalPublicationReview, approve: boolean) {
  if (busy.value) return
  const token = getToken(), seq = generation
  busy.value = true; error.value = ''; notice.value = ''
  try {
    await decidePersonalPublication(row.id, { approve, reason: reason.value.trim() }, token)
    if (!alive || seq !== generation || token !== getToken()) return
    reason.value = ''; notice.value = approve ? '已批准。' : '已拒绝。'; busy.value = false; await load()
  } catch {
    if (alive && seq === generation && token === getToken()) error.value = '审核结果未知，请刷新核对。'
  } finally { if (alive && seq === generation) busy.value = false }
}
async function revoke(row: PersonalPublicationReview) {
  if (busy.value || !window.confirm('确定下架这个 Agent 吗？')) return
  const token = getToken(), seq = generation
  busy.value = true; error.value = ''; notice.value = ''
  try {
    await revokePersonalPublication(row.id, reason.value.trim(), token)
    if (!alive || seq !== generation || token !== getToken()) return
    reason.value = ''; notice.value = '已下架。'; busy.value = false; await load()
  } catch {
    if (alive && seq === generation && token === getToken()) error.value = '下架结果未知，请刷新核对。'
  } finally { if (alive && seq === generation) busy.value = false }
}
watch(status, () => { rows.value = []; notice.value = ''; void load() })
watch(() => [user.profile?.id, user.token] as const, () => {
  ++generation; rows.value = []; reason.value = ''; notice.value = ''; error.value = ''; busy.value = false; void load()
}, { immediate: true, flush: 'sync' })
onBeforeUnmount(() => { alive = false; ++generation })
</script>

<template>
  <section aria-label="个人 Agent 审核">
    <nav class="tabs" aria-label="个人发布状态">
      <button v-for="option in options" :key="option.value" type="button" :data-status="option.value"
        :aria-pressed="status === option.value" :disabled="busy" @click="status = option.value">{{ option.label }}</button>
      <button type="button" :disabled="loading || busy" @click="load()">刷新</button>
    </nav>
    <label for="personal-review-reason">审核说明</label>
    <input id="personal-review-reason" v-model="reason" maxlength="500" />
    <p v-if="error" role="alert" class="error">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="loading" role="status">正在加载…</p><p v-else-if="!rows.length">暂无记录。</p>
    <article v-for="row in rows" :key="row.id" class="record">
      <h3>{{ row.title }}</h3><p>版本 {{ row.version_number }} · 申请人 #{{ row.owner_id }}</p>
      <details><summary>查看申请快照</summary><pre>{{ row.prompt }}</pre><small>摘要：{{ row.content_hash }}</small></details>
      <p v-if="row.decision_reason">审核说明：{{ row.decision_reason }}</p>
      <p v-if="row.revocation_reason">下架说明：{{ row.revocation_reason }}</p>
      <div class="actions">
        <button v-if="row.status === 'pending'" type="button" :data-approve="row.id" :disabled="busy" @click="decide(row, true)">批准</button>
        <button v-if="row.status === 'pending'" type="button" :disabled="busy" @click="decide(row, false)">拒绝</button>
        <button v-if="row.status === 'approved'" type="button" :data-revoke="row.id" :disabled="busy" @click="revoke(row)">下架</button>
      </div>
    </article>
    <button v-if="more" type="button" :disabled="loading || busy" @click="load(true)">加载更多</button>
  </section>
</template>

<style scoped>
.tabs, .actions { display:flex; flex-wrap:wrap; gap:8px; margin:16px 0; }
button,input { font:inherit; } button { cursor:pointer; } button:disabled { opacity:.55; cursor:not-allowed; }
.tabs button[aria-pressed='true'] { font-weight:600; } label { display:block; } input { max-width:520px; width:100%; }
.record { border-top:1px solid var(--el-border-color); padding:16px 0; } h3 { margin:0; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; font:inherit; } small { overflow-wrap:anywhere; }
.error { color:var(--el-color-danger); }
</style>
