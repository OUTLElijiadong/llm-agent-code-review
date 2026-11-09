<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import AgentRunHistory from '@/components/agent/AgentRunHistory.vue'
import { getEnterpriseAssetRun, getEnterpriseAssetRunByRequest, runEnterpriseAsset,
  type EnterpriseAssetRun } from '@/api/enterpriseAssetRuns'
import { useUserStore } from '@/stores/user'
import { getToken } from '@/utils/token'

interface Item { id: number; title: string; version_number: number; kind: 'agent' | 'skill'; personal_execution_available?: boolean }
interface Pointer { publication_id: number; title: string; version_number: number; kind: 'agent' | 'skill';
  request_key: string; input_hash: string; run_id?: number }
const props = defineProps<{ items: Item[] }>()
const user = useUserStore()
const saved = ref<Pointer[]>([]), selected = ref<Item | null>(null), pointer = ref<Pointer | null>(null)
const heading = ref<HTMLElement | null>(null)
const run = ref<EnterpriseAssetRun | null>(null), input = ref(''), error = ref('')
const busy = ref(false), uncertain = ref(false)
let generation = 0, timer: ReturnType<typeof setTimeout> | null = null
const keyFor = (id: number) => `prism:enterprise-public-runs:${id}`
const current = (seq: number, token: string | null, account: number | undefined) =>
  seq === generation && token === getToken() && token === user.token && account === user.profile?.id
const valid = (value: unknown): value is Pointer => {
  if (!value || typeof value !== 'object') return false
  const row = value as Record<string, unknown>
  return Number.isSafeInteger(row.publication_id) && Number(row.publication_id) > 0
    && Number.isSafeInteger(row.version_number) && Number(row.version_number) > 0
    && typeof row.title === 'string' && row.title.length <= 120 && (row.kind === 'agent' || row.kind === 'skill')
    && typeof row.request_key === 'string' && /^[a-z0-9][a-z0-9._:-]{0,79}$/.test(row.request_key)
    && typeof row.input_hash === 'string' && /^[a-f0-9]{1,64}$/.test(row.input_hash)
    && (row.run_id === undefined || (Number.isSafeInteger(row.run_id) && Number(row.run_id) > 0))
}
function stop() { if (timer !== null) clearTimeout(timer); timer = null }
function reset() {
  ++generation; stop(); saved.value = []; selected.value = null; pointer.value = null
  run.value = null; input.value = ''; error.value = ''; busy.value = false; uncertain.value = false
}
function readSaved() {
  const id = user.profile?.id
  if (!id || !user.token || user.token !== getToken()) return
  try {
    const text = sessionStorage.getItem(keyFor(id))
    if (!text) return
    const values: unknown = JSON.parse(text)
    if (!Array.isArray(values) || !values.every(valid)) throw new Error('invalid records')
    saved.value = values
  } catch { error.value = '本地运行记录无法读取，请查看运行记录。' }
}
function persist(item: Pointer) {
  const id = user.profile?.id
  if (!id) throw new Error('session missing')
  const items = [...saved.value.filter(row => row.request_key !== item.request_key), item]
  const text = JSON.stringify(items)
  sessionStorage.setItem(keyFor(id), text)
  if (sessionStorage.getItem(keyFor(id)) !== text) throw new Error('storage unavailable')
  saved.value = items; pointer.value = item
}
function hasUnsavedInput() { return Boolean(input.value.trim()) && !run.value }
function mayLeave() { return user.token !== getToken() || !hasUnsavedInput() || window.confirm('当前输入尚未提交，确定切换吗？') }
function choose(item: Item | Pointer) {
  if (busy.value || !mayLeave()) return
  ++generation; stop(); input.value = ''; run.value = null; error.value = ''; uncertain.value = false
  const id = 'publication_id' in item ? item.publication_id : item.id
  selected.value = props.items.find(row => row.id === id) ??
    ('publication_id' in item ? { id, title: item.title, version_number: item.version_number, kind: item.kind,
      personal_execution_available: false } : item)
  pointer.value = 'publication_id' in item ? item : [...saved.value].reverse().find(row => row.publication_id === id) ?? null
  void nextTick(() => heading.value?.focus())
  if (pointer.value) void refresh()
}
defineExpose({ choose, mayLeave })
function schedule() {
  stop()
  if (document.visibilityState === 'visible' && run.value?.status === 'running' && pointer.value?.run_id)
    timer = setTimeout(() => { timer = null; void refresh() }, 10000)
}
async function refresh() {
  const item = selected.value, savedPointer = pointer.value, token = getToken(), account = user.profile?.id, seq = generation
  if (!item || !savedPointer || !token || !account || token !== user.token || busy.value) return
  busy.value = true; error.value = ''
  try {
    const result = savedPointer.run_id ? await getEnterpriseAssetRun(item.id, savedPointer.run_id, token)
      : await getEnterpriseAssetRunByRequest(item.id, savedPointer.request_key, token)
    if (!current(seq, token, account) || selected.value?.id !== item.id) return
    if (result.enterprise_publication_id !== item.id || result.version_number !== savedPointer.version_number)
      throw new Error('run mismatch')
    persist({ ...savedPointer, run_id: result.id }); run.value = result; uncertain.value = false
  } catch (cause) {
    if (!current(seq, token, account)) return
    const code = cause && typeof cause === 'object' && 'code' in cause ? cause.code : undefined
    if (!savedPointer.run_id && (code === 404 || (typeof code === 'number' && code >= 40400 && code < 40500))) {
      uncertain.value = true; error.value = '未查到请求，可输入原内容后重试。'
    } else { run.value = null; error.value = '运行状态暂不可读取，请重试查询。' }
  } finally { if (current(seq, token, account)) { busy.value = false; schedule() } }
}
async function start() {
  const item = selected.value, token = getToken(), account = user.profile?.id, text = input.value.trim(), seq = generation
  if (!item || !account || !token || token !== user.token || busy.value || !item.personal_execution_available ||
      (pointer.value && !uncertain.value)) return
  if (!text || Array.from(text).length > 12000 || Array.from(text).some(char => {
    const code = char.codePointAt(0)!
    return (code < 32 || (code >= 127 && code <= 159)) && code !== 9 && code !== 10 && code !== 13
  })) { error.value = '请输入不超过 12000 字符的有效内容。'; return }
  busy.value = true; error.value = ''
  try {
    const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify({ input_text: text })))
    const hash = Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, '0')).join('')
    if (!current(seq, token, account)) return
    let request = pointer.value
    if (request && request.input_hash !== hash) { error.value = '内容与原请求不同，请先核对状态。'; return }
    if (!request) {
      request = { publication_id: item.id, title: item.title, version_number: item.version_number, kind: item.kind,
        request_key: crypto.randomUUID(), input_hash: hash }
      persist(request)
    }
    const result = await runEnterpriseAsset(item.id, { request_key: request.request_key, input_text: text }, token)
    if (!current(seq, token, account) || selected.value?.id !== item.id) return
    if (result.enterprise_publication_id !== item.id || result.version_number !== request.version_number)
      throw new Error('run mismatch')
    persist({ ...request, run_id: result.id }); run.value = result; uncertain.value = false
  } catch {
    if (current(seq, token, account)) { uncertain.value = Boolean(pointer.value); error.value = pointer.value
      ? '运行结果未知，可查询状态或用原内容重试。' : '无法保存请求标识，请检查浏览器存储。' }
  } finally { if (current(seq, token, account)) { busy.value = false; schedule() } }
}
function nextRun() {
  if (!pointer.value || !run.value || !['succeeded', 'result_unavailable'].includes(run.value.status)) return
  pointer.value = null; run.value = null; input.value = ''; error.value = ''
}
function visibility() { stop(); if (document.visibilityState === 'visible' && run.value?.status === 'running') void refresh() }
function beforeUnload(event: BeforeUnloadEvent) {
  if (!hasUnsavedInput() || user.token !== getToken()) return
  event.preventDefault(); event.returnValue = ''
}
onBeforeRouteLeave(() => mayLeave())
watch(() => [user.profile?.id, user.token] as const, () => { reset(); readSaved() }, { immediate: true, flush: 'sync' })
watch(() => props.items, items => {
  if (!selected.value) return
  const latest = items.find(item => item.id === selected.value?.id)
  selected.value = latest ?? { ...selected.value, personal_execution_available: false }
})
onMounted(() => { document.addEventListener('visibilitychange', visibility); window.addEventListener('beforeunload', beforeUnload) })
onBeforeUnmount(() => { ++generation; stop(); document.removeEventListener('visibilitychange', visibility)
  window.removeEventListener('beforeunload', beforeUnload) })
</script>

<template>
  <section class="asset-runner" aria-label="使用企业共享资产">
    <div v-if="saved.length" class="saved">
      <button v-for="item in saved" :key="item.request_key" type="button" :data-saved-publication="item.publication_id"
        :disabled="busy" @click="choose(item)">{{ item.title }} · v{{ item.version_number }} · 查询记录</button>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <section v-if="selected" class="run" aria-label="使用共享资产">
      <h2 ref="heading" tabindex="-1">{{ selected.title }}</h2>
      <p v-if="!selected.personal_execution_available && !pointer">暂不可运行。</p>
      <p v-if="run" role="status">{{ ({ running: '运行中', succeeded: '已完成', awaiting_reconciliation: '用量待核对',
        result_unavailable: '本次没有可展示的完整结果' })[run.status] }} · {{ run.actual_milli === null ? '费用待确认' : `${(run.actual_milli / 1000).toLocaleString('zh-CN')} 积分` }}</p>
      <pre v-if="run?.output">{{ run.output }}</pre>
      <label for="enterprise-asset-input">输入内容</label>
      <textarea id="enterprise-asset-input" v-model="input" rows="5"
        :disabled="busy || (!selected.personal_execution_available && !pointer) || Boolean(pointer && !uncertain)" />
      <div class="actions">
        <button v-if="selected.personal_execution_available && (!pointer || uncertain)" type="button" data-action="run-enterprise-public"
          :disabled="busy || !input.trim()" @click="start">{{ uncertain ? '重试同一请求' : '运行（个人积分）' }}</button>
        <button v-if="pointer" type="button" :disabled="busy" @click="refresh">查询状态</button>
        <button v-if="selected.personal_execution_available && run && ['succeeded', 'result_unavailable'].includes(run.status)"
          type="button" :disabled="busy" @click="nextRun">新建运行</button>
      </div>
    </section>
    <AgentRunHistory source="enterprise_public" />
  </section>
</template>

<style scoped>
.asset-runner { margin: 24px 0; }
.saved, .actions { display:flex; flex-wrap:wrap; gap:8px; }
.run { display:flex; flex-direction:column; gap:10px; max-width:720px; margin-top:16px; }
h2 { margin:0; font-size:18px; }
button, textarea { font:inherit; }
button { cursor:pointer; }
button:disabled { opacity:.55; cursor:not-allowed; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; font:inherit; }
.error { color:var(--el-color-danger); }
</style>
