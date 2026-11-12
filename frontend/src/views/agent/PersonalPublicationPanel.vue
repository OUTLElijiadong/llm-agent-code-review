<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { getToken } from '@/utils/token'
import { listPersonalPublications, submitPersonalPublication, withdrawPersonalPublication,
  type PersonalPublication } from '@/api/personalPublications'

const props = defineProps<{ userId: number; agentId: number; version: number; available: boolean }>()
const rows = ref<PersonalPublication[]>([])
const error = ref(''), notice = ref('')
const busy = ref(false), loading = ref(false), more = ref(false)
const uncertain = ref<{ request_key: string; version: number } | null>(null)
const storageKey = `prism:personal-publication:${props.userId}:${props.agentId}`
let generation = 0
const labels: Record<string, string> = { pending: '待审核', approved: '已发布', rejected: '已拒绝',
  withdrawn: '已撤回', revoked: '已下架' }

async function load(append = false) {
  const sequence = ++generation, token = getToken(), offset = append ? rows.value.length : 0
  loading.value = true
  try {
    // Pagination is over all owned publications; filter only after retaining the cursor.
    const page = await listPersonalPublications(token, offset)
    if (sequence !== generation || token !== getToken()) return
    rows.value = append ? [...rows.value, ...page] : page
    more.value = page.length === 100
  } catch {
    if (sequence === generation && token === getToken()) error.value = '申请记录加载失败，请重试。'
  } finally { if (sequence === generation) loading.value = false }
}
async function submit() {
  if (busy.value || (!props.available && !uncertain.value)) return
  const token = getToken(), sequence = generation
  if (!token) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    let pending = uncertain.value
    if (!pending) {
      pending = { request_key: crypto.randomUUID(), version: props.version }
      sessionStorage.setItem(storageKey, JSON.stringify(pending))
      if (sessionStorage.getItem(storageKey) !== JSON.stringify(pending)) throw new Error('storage unavailable')
      uncertain.value = pending
    }
    const result = await submitPersonalPublication(props.agentId,
      { expected_version: pending.version, request_key: pending.request_key }, token)
    if (sequence !== generation || token !== getToken()) return
    if (result.agent_id !== props.agentId || result.version_number !== pending.version) throw new Error('request mismatch')
    sessionStorage.removeItem(storageKey); uncertain.value = null; notice.value = '申请已提交。'; busy.value = false
    await load()
  } catch {
    if (sequence === generation && token === getToken()) error.value = uncertain.value
      ? '申请结果未知，可用原请求重试。' : '无法保存请求标识，请检查浏览器存储。'
  } finally { if (sequence === generation) busy.value = false }
}
async function withdraw(row: PersonalPublication) {
  if (busy.value || !window.confirm('确定撤回这项申请吗？')) return
  const token = getToken(), sequence = generation
  busy.value = true; error.value = ''; notice.value = ''
  try {
    await withdrawPersonalPublication(row.id, token)
    if (sequence !== generation || token !== getToken()) return
    notice.value = '申请已撤回。'; busy.value = false; await load()
  } catch {
    if (sequence === generation && token === getToken()) error.value = '撤回结果未知，请刷新核对。'
  } finally { if (sequence === generation) busy.value = false }
}
onMounted(() => {
  try {
    const saved = sessionStorage.getItem(storageKey)
    if (saved) {
      const parsed: unknown = JSON.parse(saved)
      if (!parsed || typeof parsed !== 'object' || !('request_key' in parsed) || !('version' in parsed)
          || typeof parsed.request_key !== 'string' || !/^[a-z0-9][a-z0-9._:-]{0,79}$/.test(parsed.request_key)
          || !Number.isSafeInteger(parsed.version) || Number(parsed.version) < 1) throw new Error('invalid pointer')
      uncertain.value = { request_key: parsed.request_key, version: Number(parsed.version) }
    }
  } catch { error.value = '本地申请记录无法读取，请检查后再提交。'; busy.value = true }
  void load()
})
onBeforeUnmount(() => { ++generation })
</script>

<template>
  <section class="publications" aria-label="全局发布">
    <div class="heading"><h3>全局发布</h3>
      <button v-if="available || uncertain" type="button" data-action="publish" :disabled="busy" @click="submit">
        {{ uncertain ? `重试版本 ${uncertain.version}` : '申请发布' }}
      </button>
    </div>
    <p v-if="error" role="alert">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="loading" role="status">正在加载…</p>
    <ul v-if="rows.some(row => row.agent_id === agentId)">
      <li v-for="row in rows.filter(item => item.agent_id === agentId)" :key="row.id">
        版本 {{ row.version_number }} · {{ labels[row.status] || row.status }}
        <span v-if="row.decision_reason"> · {{ row.decision_reason }}</span>
        <span v-if="row.revocation_reason"> · {{ row.revocation_reason }}</span>
        <button v-if="row.status === 'pending'" type="button" :data-withdraw="row.id" :disabled="busy" @click="withdraw(row)">撤回</button>
      </li>
    </ul>
    <button v-if="more" type="button" :disabled="loading" @click="load(true)">加载更多</button>
  </section>
</template>

<style scoped>
.publications { border-top: 1px solid var(--el-border-color); padding-top: 16px; }
.heading { display:flex; justify-content:space-between; align-items:center; gap:12px; }
h3 { margin:0; } ul { padding-left:20px; } li { margin-top:8px; }
button { font:inherit; cursor:pointer; } button:disabled { opacity:.55; cursor:not-allowed; }
</style>
