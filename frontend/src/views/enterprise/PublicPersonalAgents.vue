<script setup lang="ts">
import AgentRunHistory from '@/components/agent/AgentRunHistory.vue'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { getToken } from '@/utils/token'
import { getPublicAgentRun, getPublicAgentRunByRequest, listPublicAgents, runPublicAgent,
  type PublicAgent, type PublicAgentRun } from '@/api/personalPublications'

interface Pointer { publication_id: number; title: string; version_number: number; request_key: string;
  input_hash: string; run_id?: number }
const user = useUserStore()
const agents = ref<PublicAgent[]>([]), saved = ref<Pointer[]>([])
const selected = ref<PublicAgent | null>(null), pointer = ref<Pointer | null>(null), run = ref<PublicAgentRun | null>(null)
const input = ref(''), error = ref(''), loading = ref(false), busy = ref(false), uncertain = ref(false), more = ref(false)
let generation = 0, timer: ReturnType<typeof setTimeout> | null = null
const storeKey = (id: number) => `prism:public-agent-runs:${id}`
const current = (seq: number, token: string | null) => seq === generation && token === getToken()
const valid = (v: unknown): v is Pointer => {
  if (!v || typeof v !== 'object') return false
  const x = v as Record<string, unknown>
  return Number.isSafeInteger(x.publication_id) && Number(x.publication_id) > 0
    && Number.isSafeInteger(x.version_number) && Number(x.version_number) > 0
    && typeof x.title === 'string' && x.title.length <= 120
    && typeof x.request_key === 'string' && /^[a-z0-9][a-z0-9._:-]{0,79}$/.test(x.request_key)
    && typeof x.input_hash === 'string' && /^[a-f0-9]{1,64}$/.test(x.input_hash)
    && (x.run_id === undefined || (Number.isSafeInteger(x.run_id) && Number(x.run_id) > 0))
}
function stop() { if (timer !== null) clearTimeout(timer); timer = null }
function reset() {
  ++generation; stop(); agents.value = []; saved.value = []; selected.value = null; pointer.value = null
  run.value = null; input.value = ''; error.value = ''; loading.value = false; busy.value = false; uncertain.value = false
  more.value = false
}
function persist(item: Pointer) {
  const id = user.profile?.id
  if (!id) throw new Error('session missing')
  const items = [...saved.value.filter(row => row.request_key !== item.request_key), item]
  const text = JSON.stringify(items)
  sessionStorage.setItem(storeKey(id), text)
  if (sessionStorage.getItem(storeKey(id)) !== text) throw new Error('storage unavailable')
  saved.value = items; pointer.value = item
}
async function load(append = false) {
  const token = getToken(), seq = generation, id = user.profile?.id
  if (!token || token !== user.token || !id) return
  loading.value = true
  try {
    const text = sessionStorage.getItem(storeKey(id))
    if (text) {
      const items: unknown = JSON.parse(text)
      if (!Array.isArray(items) || !items.every(valid)) throw new Error('invalid local records')
      saved.value = items
    }
    const items = await listPublicAgents(token, append ? agents.value.length : 0)
    if (current(seq, token)) { agents.value = append ? [...agents.value, ...items] : items; more.value = items.length === 100 }
  } catch {
    if (current(seq, token)) error.value = '目录或本地运行记录加载失败，请检查后重试。'
  } finally { if (current(seq, token)) loading.value = false }
}
function choose(item: PublicAgent | Pointer) {
  if (busy.value) return
  if (input.value.trim() && !run.value && !window.confirm('当前输入尚未提交，确定切换吗？')) return
  ++generation; stop(); input.value = ''; run.value = null; error.value = ''; uncertain.value = false
  const id = 'publication_id' in item ? item.publication_id : item.id
  selected.value = 'publication_id' in item
    ? agents.value.find(agent => agent.id === id)
      ?? { id, title: item.title, version_number: item.version_number, execution_available: false } : item
  pointer.value = 'publication_id' in item ? item : [...saved.value].reverse().find(row => row.publication_id === id) ?? null
  if (pointer.value) void refresh()
}
function schedule() {
  stop()
  if (document.visibilityState === 'visible' && run.value?.status === 'running' && pointer.value?.run_id)
    timer = setTimeout(() => { timer = null; void refresh() }, 10000)
}
async function refresh() {
  const item = selected.value, savedPointer = pointer.value, token = getToken(), seq = generation
  if (!item || !savedPointer || !token || busy.value) return
  busy.value = true; error.value = ''
  try {
    const result = savedPointer.run_id
      ? await getPublicAgentRun(item.id, savedPointer.run_id, token)
      : await getPublicAgentRunByRequest(item.id, savedPointer.request_key, token)
    if (!current(seq, token) || selected.value?.id !== item.id) return
    if (result.publication_id !== item.id || result.agent_id !== null
        || result.version_number !== savedPointer.version_number) throw new Error('run mismatch')
    persist({ ...savedPointer, run_id: result.id }); run.value = result; uncertain.value = false
  } catch (cause) {
    if (!current(seq, token)) return
    const code = cause && typeof cause === 'object' && 'code' in cause ? cause.code : undefined
    if (!savedPointer.run_id && (code === 404 || (typeof code === 'number' && code >= 40400 && code < 40500))) {
      uncertain.value = true; error.value = '未查到请求，可输入原内容后重试。'
    } else { run.value = null; error.value = '运行状态暂不可读取，请重试查询。' }
  } finally { if (current(seq, token)) { busy.value = false; schedule() } }
}
async function start() {
  const item = selected.value, token = getToken(), id = user.profile?.id, text = input.value.trim(), seq = generation
  if (!item || !id || !token || busy.value || !item.execution_available || (pointer.value && !uncertain.value)) return
  if (!text || Array.from(text).length > 12000 || Array.from(text).some(char => {
    const code = char.codePointAt(0)!
    return (code < 32 || (code >= 127 && code <= 159)) && code !== 9 && code !== 10 && code !== 13
  })) { error.value = '请输入不超过 12000 字符的有效内容。'; return }
  busy.value = true; error.value = ''
  try {
    const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify({ input_text: text })))
    const hash = Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, '0')).join('')
    if (!current(seq, token)) return
    let request = pointer.value
    if (request && request.input_hash !== hash) { error.value = '内容与原请求不同，请先核对状态。'; return }
    if (!request) {
      request = { publication_id: item.id, title: item.title, version_number: item.version_number,
        request_key: crypto.randomUUID(), input_hash: hash }
      persist(request)
    }
    const result = await runPublicAgent(item.id, { request_key: request.request_key, input_text: text }, token)
    if (!current(seq, token) || selected.value?.id !== item.id) return
    if (result.publication_id !== item.id || result.agent_id !== null
        || result.version_number !== request.version_number) throw new Error('run mismatch')
    persist({ ...request, run_id: result.id }); run.value = result; uncertain.value = false
  } catch {
    if (current(seq, token)) { uncertain.value = Boolean(pointer.value); error.value = pointer.value
      ? '运行结果未知，可查询状态或用原内容重试。' : '无法保存请求标识，请检查浏览器存储。' }
  } finally { if (current(seq, token)) { busy.value = false; schedule() } }
}
function nextRun() {
  if (!pointer.value || !user.profile?.id || !run.value || !['succeeded', 'result_unavailable'].includes(run.value.status)) return
  pointer.value = null; run.value = null; input.value = ''; error.value = ''
}
function visibility() { stop(); if (document.visibilityState === 'visible' && run.value?.status === 'running') void refresh() }
function hasUnsavedInput() { return Boolean(input.value.trim()) && !run.value }
function beforeUnload(event: BeforeUnloadEvent) {
  if (!hasUnsavedInput() || user.token !== getToken()) return
  event.preventDefault(); event.returnValue = ''
}
onBeforeRouteLeave(() => user.token !== getToken() || !hasUnsavedInput()
  || window.confirm('当前输入尚未保存，确定离开吗？'))
watch(() => [user.profile?.id, user.token] as const, () => { reset(); void load() }, { immediate: true, flush: 'sync' })
onMounted(() => { document.addEventListener('visibilitychange', visibility); window.addEventListener('beforeunload', beforeUnload) })
onBeforeUnmount(() => { ++generation; stop(); document.removeEventListener('visibilitychange', visibility)
  window.removeEventListener('beforeunload', beforeUnload) })
</script>

<template>
  <section class="public-agents" aria-label="个人共享 Agent">
    <h2>个人共享 Agent</h2>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="loading" role="status">正在加载…</p>
    <div class="agent-list">
      <button v-for="item in agents" :key="item.id" type="button" :data-publication="item.id" :disabled="busy" @click="choose(item)">
        {{ item.title }}
      </button>
      <button v-for="item in saved" :key="`saved-${item.request_key}`" type="button"
        :data-saved-publication="item.publication_id" :disabled="busy" @click="choose(item)">
        {{ item.title }} · v{{ item.version_number }} · 查询记录 {{ saved.indexOf(item) + 1 }}
      </button>
    </div>
    <button v-if="more" type="button" :disabled="loading" @click="load(true)">加载更多</button>
    <p v-if="!loading && !agents.length && !saved.length">暂无共享 Agent。</p>
    <section v-if="selected" class="run" aria-label="使用共享 Agent">
      <h3>{{ selected.title }}</h3>
      <p v-if="!selected.execution_available && !pointer">暂不可运行。</p>
      <p v-if="run" role="status">{{ ({ running: '运行中', succeeded: '已完成', awaiting_reconciliation: '用量待核对',
        result_unavailable: '本次没有可展示的完整结果' })[run.status] }} · {{ run.actual_milli === null ? '费用待确认' : `${(run.actual_milli / 1000).toLocaleString('zh-CN')} 积分` }}</p>
      <pre v-if="run?.output">{{ run.output }}</pre>
      <label for="public-agent-input">输入内容</label>
      <textarea id="public-agent-input" v-model="input" rows="5"
        :disabled="busy || (!selected.execution_available && !pointer) || Boolean(pointer && !uncertain)" />
      <div class="actions">
        <button v-if="selected.execution_available && (!pointer || uncertain)" type="button" data-action="run-public"
          :disabled="busy || !input.trim()" @click="start">{{ uncertain ? '重试同一请求' : '运行' }}</button>
        <button v-if="pointer" type="button" :disabled="busy" @click="refresh">查询状态</button>
        <button v-if="selected.execution_available && run && ['succeeded', 'result_unavailable'].includes(run.status)" type="button" :disabled="busy" @click="nextRun">新建运行</button>
      </div>
    </section>
    <AgentRunHistory source="public" />
  </section>
</template>

<style scoped>
.public-agents { margin: 24px 0; } h2 { font-size: 18px; } h3 { margin:0; }
.agent-list, .actions { display:flex; flex-wrap:wrap; gap:8px; }
.run { display:flex; flex-direction:column; gap:10px; max-width:720px; margin-top:16px; }
button,textarea { font:inherit; } button { cursor:pointer; } button:disabled { opacity:.55; cursor:not-allowed; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; font:inherit; } .error { color:var(--el-color-danger); }
</style>
