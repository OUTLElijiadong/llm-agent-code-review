<template>
  <main class="review-page">
    <header class="page-heading">
      <h1>全局发布审核</h1>
      <button v-if="source === 'enterprise'" type="button" :disabled="loading || acting" @click="load">刷新</button>
    </header>
    <nav class="status-tabs" aria-label="申请来源">
      <button type="button" :aria-pressed="source === 'enterprise'" @click="source = 'enterprise'">企业</button>
      <button type="button" :aria-pressed="source === 'personal'" @click="source = 'personal'">个人</button>
    </nav>
    <PersonalPublicationReview v-if="source === 'personal'" />
    <template v-else>
    <nav class="status-tabs" aria-label="发布审批状态">
      <button v-for="option in statuses" :key="option.value" type="button" :aria-pressed="status === option.value"
        @click="selectStatus(option.value)">{{ option.label }}</button>
    </nav>
    <label class="reason-label" for="global-review-reason">审批或撤销说明</label>
    <input id="global-review-reason" v-model="reason" maxlength="500" placeholder="记录审核依据；可留空" />
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" class="notice" role="status">{{ notice }}</p>
    <p v-if="loading && !items.length" role="status">正在加载申请…</p>
    <p v-else-if="!items.length" class="empty">此状态下没有全局发布记录。</p>
    <section v-else class="publication-list" aria-label="全局发布记录">
      <article v-for="item in items" :key="item.id" class="publication-card">
        <header><div><h2>{{ item.title }}</h2><p>{{ kindLabel(item.kind) }} · v{{ item.version_number }} · 企业 #{{ item.enterprise_id }} · 申请人 #{{ item.requested_by }}</p></div>
          <span class="status" :class="`status-${item.status}`">{{ statusLabel(item.status) }}</span></header>
        <p class="hash">固定内容 SHA-256：{{ item.content_sha256 }}</p>
        <details><summary>查看完整申请快照</summary><pre>{{ JSON.stringify(item.definition, null, 2) }}</pre></details>
        <p v-if="item.decision_reason" class="decision-note">审批说明：{{ item.decision_reason }}</p>
        <p v-if="item.revocation_reason" class="decision-note">撤销说明：{{ item.revocation_reason }}</p>
        <div v-if="item.status === 'pending' || item.status === 'approved'" class="actions">
          <button v-if="item.status === 'pending'" type="button" class="approve" :disabled="acting" @click="decide(item, 'approve')">批准进入全局目录</button>
          <button v-if="item.status === 'pending'" type="button" :disabled="acting" @click="decide(item, 'reject')">拒绝</button>
          <button v-if="item.status === 'approved'" type="button" class="revoke" :disabled="acting" @click="revoke(item)">撤销全局发布</button>
        </div>
        <p class="execution">Agent/Skill 执行状态：关闭</p>
      </article>
    </section>
    </template>
  </main>
</template>

<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { decidePlatformEnterpriseAssetPublication, listPlatformEnterpriseAssetPublications,
  revokePlatformEnterpriseAssetPublication, type EnterpriseAssetPublication } from '@/api/enterpriseAssets'
import { getToken } from '@/utils/token'
import PersonalPublicationReview from './PersonalPublicationReview.vue'
import { useUserStore } from '@/stores/user'

type PublicationStatus = 'pending' | 'approved' | 'rejected' | 'revoked'
const statuses: Array<{ value: PublicationStatus; label: string }> = [
  { value: 'pending', label: '待审批' }, { value: 'approved', label: '已发布' },
  { value: 'rejected', label: '已拒绝' }, { value: 'revoked', label: '已撤销' },
]
const status = ref<PublicationStatus>('pending')
const source = ref<'enterprise' | 'personal'>('enterprise')
const user = useUserStore()
const items = ref<EnterpriseAssetPublication[]>([])
const reason = ref('')
const loading = ref(false), acting = ref(false), error = ref(''), notice = ref('')
let generation = 0
let alive = true

function kindLabel(kind: string) { return kind === 'agent' ? 'Agent' : 'Skill' }
function statusLabel(value: string) { return statuses.find(item => item.value === value)?.label || value }
function selectStatus(value: PublicationStatus) { status.value = value }
async function load() {
  const request = ++generation, token = getToken(), selectedStatus = status.value
  if (!token || token !== user.token || !user.profile?.id) return
  loading.value = true; error.value = ''
  try {
    const rows = await listPlatformEnterpriseAssetPublications(selectedStatus, token)
    if (request !== generation || token !== getToken()) return
    items.value = rows
  } catch {
    if (request !== generation || token !== getToken()) return
    error.value = '审批记录加载失败，请确认平台管理员权限后重试。'
  } finally { if (request === generation) loading.value = false }
}
async function decide(item: EnterpriseAssetPublication, decision: 'approve' | 'reject') {
  if (acting.value) return
  const token = getToken()
  acting.value = true; error.value = ''; notice.value = ''
  try {
    await decidePlatformEnterpriseAssetPublication(item.id, { decision, reason: reason.value.trim() }, token)
    if (!alive || token !== getToken()) return
    reason.value = ''; notice.value = decision === 'approve' ? '已批准该固定版本进入全局目录。' : '已拒绝此申请。'
    await load()
  } catch {
    if (!alive || token !== getToken()) return
    error.value = '审批结果未知，请刷新列表核对状态。'
  } finally { if (alive) acting.value = false }
}
async function revoke(item: EnterpriseAssetPublication) {
  if (acting.value) return
  const note = window.prompt('撤销说明（可留空）', '')
  if (note === null) return
  const token = getToken()
  acting.value = true; error.value = ''; notice.value = ''
  try {
    await revokePlatformEnterpriseAssetPublication(item.id, { reason: note.trim() }, token)
    if (!alive || token !== getToken()) return
    notice.value = '全局发布已撤销，目录将不再显示该版本。'; await load()
  } catch {
    if (!alive || token !== getToken()) return
    error.value = '撤销结果未知，请刷新列表核对状态。'
  } finally { if (alive) acting.value = false }
}
watch(status, () => { notice.value = ''; void load() })
watch(() => [user.profile?.id, user.token] as const, () => {
  ++generation; items.value = []; reason.value = ''; error.value = ''; notice.value = ''; acting.value = false; void load()
}, { immediate: true, flush: 'sync' })
onBeforeUnmount(() => { alive = false; ++generation })
</script>

<style scoped>
.review-page { max-width:1180px; margin:0 auto; padding:28px; }.page-heading { display:flex; align-items:center; justify-content:space-between; gap:16px; }
h1 { margin:0 0 6px; font-size:23px; }.page-heading p,.publication-card p { color:#667085; }
button,input { font:inherit; border:1px solid #d5dae5; border-radius:9px; padding:9px 12px; background:#fff; }
button { cursor:pointer; } button:disabled { cursor:default; opacity:.6; }.status-tabs { display:flex; flex-wrap:wrap; gap:8px; margin:24px 0 16px; }
.status-tabs button[aria-pressed='true'] { border-color:#5b61c8; color:#4149aa; background:#f0f1ff; }
.reason-label { display:block; margin-bottom:6px; font-size:13px; }.review-page > input { width:min(520px,100%); }
.publication-list { display:grid; gap:14px; margin-top:20px; }.publication-card { padding:18px; border:1px solid #e4e7ef; border-radius:14px; background:#fff; }
.publication-card header { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; }.publication-card h2 { margin:0 0 5px; font-size:17px; }.publication-card header p { margin:0; }
.status { white-space:nowrap; padding:4px 9px; border-radius:20px; background:#f2f4f7; font-size:12px; }.status-pending { color:#93370d; background:#fef0c7; }.status-approved { color:#027a48; background:#dcfae6; }
.hash { overflow-wrap:anywhere; font:12px/1.6 ui-monospace,monospace; }.publication-card summary { cursor:pointer; }.publication-card pre { max-height:420px; overflow:auto; padding:14px; border-radius:8px; background:#f6f7fa; white-space:pre-wrap; overflow-wrap:anywhere; }
.actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:16px; }.approve { color:#fff; border-color:#4654bb; background:#4654bb; }.revoke { color:#b42318; }.decision-note { font-size:13px; }.execution { margin-bottom:0; font-size:13px; }
.empty { padding:30px; color:#667085; text-align:center; }.error { color:#b42318; }.notice { color:#027a48; }
@media(max-width:640px) { .review-page { padding:18px; }.page-heading { align-items:flex-start; flex-direction:column; }.publication-card header { flex-direction:column; } }
</style>
