<script setup lang="ts">
import AgentRunHistory from '@/components/agent/AgentRunHistory.vue'
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { getToken } from '@/utils/token'
import PersonalPublicationPanel from './PersonalPublicationPanel.vue'
import {
  createPersonalAgent, disablePersonalAgent, getPersonalAgent, listPersonalAgents, updatePersonalAgent,
  createPersonalAgentRun, getPersonalAgentRun, getPersonalAgentRunByRequest,
  type PersonalAgent, type PersonalAgentRun,
} from '@/api/personalAgents'

const user = useUserStore()
const agents = ref<PersonalAgent[]>([])
const selected = ref<PersonalAgent | null>(null)
const editing = ref(false)
const title = ref('')
const prompt = ref('')
const loading = ref(false)
const detailLoading = ref(false)
const busy = ref(false)
const hasMore = ref(false)
const error = ref('')
const message = ref('')
const versionConflict = ref(false)
const showEditor = ref(false)
const titleField = ref<HTMLInputElement | null>(null)
const editorHeading = ref<HTMLElement | null>(null)
const listPanel = ref<HTMLElement | null>(null)
const isDirty = computed(() => editing.value && (
  title.value !== (selected.value?.title ?? '') || prompt.value !== (selected.value?.prompt ?? '')
))
interface RunPointer { request_key: string; version_number: number; input_hash: string; run_id?: number }
const runInput = ref('')
const runSubmittedInput = ref('')
const run = ref<PersonalAgentRun | null>(null)
const runPointer = ref<RunPointer | null>(null)
const runUncertain = ref(false)
const runBlocked = ref(false)
const runBusy = ref(false)
const runError = ref('')
const runUnresolved = computed(() => Boolean(runPointer.value && (!run.value
  || run.value.status === 'running' || run.value.status === 'awaiting_reconciliation')))
const runReason = computed(() => {
  const reason = run.value?.result_reason
  if (!reason) return ''
  return ({ length: '内容未完整生成', content_filter: '内容未通过供应商筛选',
    tool_calls: '返回了未支持的工具调用', insufficient_system_resource: '供应商资源不足',
    aborted: '运行已中断', invalid_response: '供应商响应无法验证', access_changed: '访问权限已变化',
    reconciled_no_result: '对账完成，但结果不可恢复' } as Record<string, string>)[reason] || '结果无法恢复'
})
let runGeneration = 0
let runPoll: ReturnType<typeof setTimeout> | null = null
const RUN_POLL_MS = 10000
let scopeGeneration = 0
let listGeneration = 0
let selectionGeneration = 0
let pageUserId: number | null = null
let pageToken: string | null = null

function isCurrent(scope: number, token: string | null): boolean {
  return scope === scopeGeneration && token === getToken()
}
function codeOf(cause: unknown): number | undefined {
  return cause && typeof cause === 'object' && 'code' in cause && typeof cause.code === 'number' ? cause.code : undefined
}
function clear(): void {
  agents.value = []; selected.value = null; editing.value = false
  title.value = ''; prompt.value = ''; error.value = ''; message.value = ''; loading.value = false; busy.value = false
  hasMore.value = false; detailLoading.value = false; showEditor.value = false; versionConflict.value = false
  clearRunView()
}
function clearRunView(): void {
  ++runGeneration
  stopRunPoll()
  runInput.value = ''; runSubmittedInput.value = ''; run.value = null; runPointer.value = null
  runUncertain.value = false; runBlocked.value = false; runBusy.value = false; runError.value = ''
}
function stopRunPoll(): void {
  if (runPoll !== null) clearTimeout(runPoll)
  runPoll = null
}
function scheduleRunPoll(): void {
  stopRunPoll()
  if (document.visibilityState !== 'visible' || runBusy.value || !runPointer.value?.run_id
      || run.value?.status !== 'running') return
  runPoll = setTimeout(() => { runPoll = null; void refreshRun() }, RUN_POLL_MS)
}
function handleVisibilityChange(): void {
  stopRunPoll()
  if (document.visibilityState === 'visible' && run.value?.status === 'running' && !runBusy.value) void refreshRun()
}
function isSameSession(): boolean {
  return pageUserId !== null && pageUserId === user.profile?.id && pageToken === user.token && pageToken === getToken()
}
async function load(more = false): Promise<void> {
  const scope = scopeGeneration, sequence = ++listGeneration, token = getToken()
  if (!token || !user.profile?.id) return
  loading.value = true
  try {
    const rows = await listPersonalAgents(token, more ? agents.value.length : 0)
    if (isCurrent(scope, token) && sequence === listGeneration) {
      agents.value = more ? [...agents.value, ...rows] : rows
      hasMore.value = rows.length === 50
    }
  } catch {
    if (isCurrent(scope, token) && sequence === listGeneration) error.value = '列表加载失败，请重试。'
  } finally {
    if (isCurrent(scope, token) && sequence === listGeneration) loading.value = false
  }
}
function mayDiscardChanges(): boolean {
  const unsentInput = Boolean(runInput.value.trim()) && runInput.value !== runSubmittedInput.value
  return (!isDirty.value && !unsentInput && !runUncertain.value)
    || window.confirm('当前修改或运行输入尚未保存，确定放弃并继续吗？')
}
async function focusEditor(): Promise<void> {
  showEditor.value = true
  await nextTick()
  if (titleField.value && !titleField.value.disabled) titleField.value.focus()
  else editorHeading.value?.focus()
}
async function showList(): Promise<void> {
  showEditor.value = false
  await nextTick()
  listPanel.value?.focus()
}
function startNew(): void {
  if (busy.value) return
  if (!mayDiscardChanges()) return
  ++selectionGeneration
  clearRunView()
  detailLoading.value = false
  selected.value = null; title.value = ''; prompt.value = ''; editing.value = true; error.value = ''; message.value = ''
  versionConflict.value = false
  void focusEditor()
}
async function selectAgent(id: number, force = false): Promise<void> {
  if (busy.value) return
  if (!force && selected.value?.id === id) { void focusEditor(); return }
  if (!mayDiscardChanges()) return
  const scope = scopeGeneration, sequence = ++selectionGeneration, token = getToken()
  clearRunView()
  detailLoading.value = true
  error.value = ''; message.value = ''; editing.value = false; selected.value = null; versionConflict.value = false
  try {
    const row = await getPersonalAgent(id, token)
    if (!isCurrent(scope, token) || sequence !== selectionGeneration) return
    selected.value = row; title.value = row.title; prompt.value = row.prompt; editing.value = true
    void focusEditor()
    void restoreRun(row.id, scope, sequence, token)
  } catch {
    if (isCurrent(scope, token) && sequence === selectionGeneration) error.value = '该 Agent 不存在或无法访问，请刷新列表。'
  } finally {
    if (isCurrent(scope, token) && sequence === selectionGeneration) detailLoading.value = false
  }
}
function reloadConflict(): void {
  if (selected.value) void selectAgent(selected.value.id, true)
}
async function contentHash(content: object): Promise<string> {
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(content)))
  return Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, '0')).join('')
}
async function pendingKey(userId: number, action: string, content: object): Promise<{ key: string; request: string }> {
  const hash = await contentHash(content)
  const key = `prism:personal-agent:${userId}:${action}:${hash}`
  let request = sessionStorage.getItem(key)
  if (!request) { request = crypto.randomUUID(); sessionStorage.setItem(key, request) }
  if (sessionStorage.getItem(key) !== request) throw new Error('storage unavailable')
  return { key, request }
}
function clearPending(key: string, request: string): void {
  try { if (sessionStorage.getItem(key) === request) sessionStorage.removeItem(key) } catch { /* 已保存 */ }
}
function hasInvalidControl(value: string, allowNewlines: boolean): boolean {
  return Array.from(value).some(char => {
    const code = char.codePointAt(0)!
    return (code < 32 || (code >= 127 && code <= 159))
      && !(allowNewlines && (code === 9 || code === 10 || code === 13))
  })
}
function pointerKey(userId: number, agentId: number): string {
  return `prism:personal-agent-run:${userId}:${agentId}`
}
function validPointer(value: unknown): value is RunPointer {
  if (!value || typeof value !== 'object') return false
  const item = value as Record<string, unknown>
  return typeof item.request_key === 'string' && /^[a-z0-9][a-z0-9._:-]{0,79}$/.test(item.request_key)
    && Number.isSafeInteger(item.version_number) && Number(item.version_number) > 0
    && typeof item.input_hash === 'string' && /^[a-f0-9]{1,64}$/.test(item.input_hash)
    && (item.run_id === undefined || (Number.isSafeInteger(item.run_id) && Number(item.run_id) > 0))
}
function savePointer(userId: number, agentId: number, pointer: RunPointer): void {
  const key = pointerKey(userId, agentId)
  const content = JSON.stringify(pointer)
  sessionStorage.setItem(key, content)
  if (sessionStorage.getItem(key) !== content) throw new Error('storage unavailable')
}
function runIsCurrent(scope: number, sequence: number, token: string | null, agentId: number): boolean {
  return isCurrent(scope, token) && sequence === runGeneration && selected.value?.id === agentId
}
async function restoreRun(agentId: number, scope: number, selection: number, token: string | null): Promise<void> {
  const userId = user.profile?.id
  if (!userId || !token) return
  const sequence = ++runGeneration
  try {
    const stored = sessionStorage.getItem(pointerKey(userId, agentId))
    if (!stored) return
    const pointer: unknown = JSON.parse(stored)
    if (!validPointer(pointer)) throw new Error('invalid pointer')
    if (!runIsCurrent(scope, sequence, token, agentId) || selection !== selectionGeneration) return
    runPointer.value = pointer
    await refreshRun()
  } catch {
    if (runIsCurrent(scope, sequence, token, agentId) && selection === selectionGeneration) {
      runBlocked.value = true
      runError.value = '本地运行记录无法读取，请联系管理员核对后再操作。'
    }
  }
}
async function refreshRun(): Promise<void> {
  const row = selected.value, pointer = runPointer.value, userId = user.profile?.id, token = getToken()
  if (!row || !pointer || !userId || !token || runBusy.value) return
  const scope = scopeGeneration, sequence = ++runGeneration
  runBusy.value = true; runError.value = ''
  try {
    const result = pointer.run_id
      ? await getPersonalAgentRun(row.id, pointer.run_id, token)
      : await getPersonalAgentRunByRequest(row.id, pointer.request_key, token)
    if (!runIsCurrent(scope, sequence, token, row.id)) return
    if (result.agent_id !== row.id || result.version_number !== pointer.version_number) throw new Error('run mismatch')
    const updated = { ...pointer, run_id: result.id }
    savePointer(userId, row.id, updated)
    runPointer.value = updated; run.value = result; runUncertain.value = false
  } catch (cause) {
    if (!runIsCurrent(scope, sequence, token, row.id)) return
    const code = codeOf(cause)
    const forbidden = code === 403 || (code !== undefined && code >= 40300 && code < 40400)
    const notFound = code === 404 || (code !== undefined && code >= 40400 && code < 40500)
    if (forbidden || (pointer.run_id && notFound)) {
      run.value = null; runInput.value = ''; runSubmittedInput.value = ''
    }
    if (!pointer.run_id && notFound) runUncertain.value = true
    if (forbidden) runUncertain.value = false
    runError.value = runUncertain.value ? '尚未查到该请求，可输入原内容后使用同一请求重试。' : '运行状态暂不可读取，请重试查询。'
  } finally { if (runIsCurrent(scope, sequence, token, row.id)) runBusy.value = false }
}
async function startRun(): Promise<void> {
  const row = selected.value, userId = user.profile?.id, token = getToken(), input = runInput.value.trim()
  if (!row || !userId || !token || row.execution_available !== true || row.status !== 'active'
      || busy.value || isDirty.value || runBlocked.value || runBusy.value || (runPointer.value && !runUncertain.value)) return
  if (!input || Array.from(input).length > 12000 || hasInvalidControl(input, true)) {
    runError.value = '请输入不超过 12000 字符的有效内容。'; return
  }
  const scope = scopeGeneration, sequence = ++runGeneration
  runBusy.value = true; runError.value = ''
  let sent = false
  try {
    const content = { version_number: runPointer.value?.version_number ?? row.current_version, input_text: input }
    const hash = await contentHash(content)
    if (!runIsCurrent(scope, sequence, token, row.id)) return
    let pointer = runPointer.value
    if (pointer && pointer.input_hash !== hash) {
      runError.value = '内容或版本与原请求不同，请先核对原请求状态。'; return
    }
    if (!pointer) {
      const pending = await pendingKey(userId, `run:${row.id}`, content)
      if (!runIsCurrent(scope, sequence, token, row.id)) return
      pointer = { request_key: pending.request, version_number: row.current_version, input_hash: hash }
      savePointer(userId, row.id, pointer)
      runPointer.value = pointer
    }
    sent = true
    const result = await createPersonalAgentRun(row.id, {
      request_key: pointer.request_key, version_number: pointer.version_number, input_text: input,
    }, token)
    if (!runIsCurrent(scope, sequence, token, row.id)) return
    if (result.agent_id !== row.id || result.version_number !== pointer.version_number) throw new Error('run mismatch')
    const updated = { ...pointer, run_id: result.id }
    savePointer(userId, row.id, updated)
    runPointer.value = updated; run.value = result; runSubmittedInput.value = runInput.value; runUncertain.value = false
  } catch (cause) {
    if (!runIsCurrent(scope, sequence, token, row.id)) return
    runUncertain.value = sent
    const code = codeOf(cause)
    if (!sent) runError.value = '无法保存运行标识，请检查浏览器存储设置。'
    else if (code === 40200) runError.value = '积分或权益不足，请充值后查询状态或重试同一请求。'
    else if (code === 50301) runError.value = '平台服务暂不可用，请稍后查询状态或重试同一请求。'
    else if (code === 409 || code === 40901) runError.value = '请求状态或版本冲突，请先查询状态。'
    else runError.value = '运行结果未知，可查询状态或重试同一请求。'
  } finally { if (runIsCurrent(scope, sequence, token, row.id)) runBusy.value = false }
}
function newRun(): void {
  const row = selected.value, userId = user.profile?.id
  if (!row || !userId || !run.value || !['succeeded', 'result_unavailable'].includes(run.value.status)) return
  try {
    sessionStorage.removeItem(pointerKey(userId, row.id))
    const pending = `prism:personal-agent:${userId}:run:${row.id}:${runPointer.value?.input_hash}`
    sessionStorage.removeItem(pending)
  } catch { runError.value = '无法更新本地运行记录，请检查浏览器存储设置。'; return }
  clearRunView()
}
async function save(): Promise<void> {
  if (busy.value || runUnresolved.value || selected.value?.status === 'disabled') return
  const name = title.value.trim(), instructions = prompt.value.trim(), userId = user.profile?.id
  if (!name || !instructions || !userId) { error.value = '请输入名称和指令。'; return }
  if (Array.from(name).length > 120 || Array.from(instructions).length > 30000) {
    error.value = '名称最多 120 字符，指令最多 30000 字符。'; return
  }
  if (hasInvalidControl(name, false) || hasInvalidControl(instructions, true)) {
    error.value = '名称或指令包含不允许的控制字符。'; return
  }
  const scope = scopeGeneration, token = getToken(), row = selected.value
  busy.value = true; error.value = ''; message.value = ''; versionConflict.value = false
  let pending: { key: string; request: string } | undefined
  try {
    pending = await pendingKey(userId, row ? `revise:${row.id}` : 'create',
      { title: name, prompt: instructions, expected_version: row?.current_version ?? null })
    if (!isCurrent(scope, token)) return
    const saved = row
      ? await updatePersonalAgent(row.id, { title: name, prompt: instructions,
        expected_version: row.current_version, request_key: pending.request }, token)
      : await createPersonalAgent({ title: name, prompt: instructions, request_key: pending.request }, token)
    clearPending(pending.key, pending.request)
    if (!isCurrent(scope, token)) return
    selected.value = saved; title.value = saved.title; prompt.value = saved.prompt
    message.value = '已保存。'; await load()
  } catch (cause) {
    if (!isCurrent(scope, token)) return
    if ([409, 40901].includes(codeOf(cause) ?? 0)) {
      error.value = '版本已变化，请重新加载核对；当前编辑内容仍保留。'
      versionConflict.value = true
    }
    else if (!pending) error.value = '浏览器无法保存重试标识，请检查浏览器存储设置。'
    else error.value = '保存结果未知，可重试相同内容或刷新后核对。'
  } finally { if (isCurrent(scope, token)) busy.value = false }
}
async function disable(): Promise<void> {
  const row = selected.value
  if (!row || row.status !== 'active' || busy.value || runUnresolved.value) return
  const question = isDirty.value
    ? `当前修改尚未保存，停用会放弃修改。确定停用“${row.title}”吗？`
    : `确定停用“${row.title}”吗？`
  if (!window.confirm(question)) return
  const scope = scopeGeneration, token = getToken()
  busy.value = true; error.value = ''; message.value = ''; versionConflict.value = false
  try {
    const saved = await disablePersonalAgent(row.id, { expected_version: row.current_version }, token)
    if (!isCurrent(scope, token)) return
    selected.value = saved; title.value = saved.title; prompt.value = saved.prompt; message.value = '已停用。'; await load()
  } catch (cause) {
    if (!isCurrent(scope, token)) return
    versionConflict.value = [409, 40901].includes(codeOf(cause) ?? 0)
    error.value = versionConflict.value ? '版本已变化，请重新加载核对。' : '停用结果未知，请刷新后核对。'
  } finally { if (isCurrent(scope, token)) busy.value = false }
}
watch(() => [user.profile?.id ?? null, user.token] as const, () => {
  ++scopeGeneration; ++selectionGeneration; ++listGeneration; clear()
  pageUserId = user.profile?.id ?? null; pageToken = user.token || null
  void load()
}, { immediate: true, flush: 'sync' })
watch([() => run.value?.status, () => runPointer.value?.run_id, runBusy], scheduleRunPoll, { flush: 'post' })
function beforeUnload(event: BeforeUnloadEvent): void {
  if (!isSameSession() || (!isDirty.value && !runUncertain.value
      && (!runInput.value.trim() || runInput.value === runSubmittedInput.value))) return
  event.preventDefault()
  event.returnValue = ''
}
onBeforeRouteLeave(() => !isSameSession() || mayDiscardChanges())
onMounted(() => {
  window.addEventListener('beforeunload', beforeUnload)
  document.addEventListener('visibilitychange', handleVisibilityChange)
})
onUnmounted(() => {
  ++scopeGeneration; ++selectionGeneration; ++listGeneration
  stopRunPoll()
  window.removeEventListener('beforeunload', beforeUnload)
  document.removeEventListener('visibilitychange', handleVisibilityChange)
})
</script>

<template>
  <section class="personal-agents">
    <header class="heading">
      <h2>我的 Agent</h2>
      <button type="button" data-action="new" :disabled="busy" @click="startNew">新建 Agent</button>
    </header>
    <p class="notice">个人配置仅自己可见<span v-if="selected && !selected.execution_available">，运行暂不可用</span>。</p>
    <p v-if="error" class="error" role="alert">{{ error }}
      <button v-if="versionConflict && selected" type="button" data-action="reload-conflict" @click="reloadConflict">重新加载</button>
    </p>
    <p v-if="message" class="message" role="status">{{ message }}</p>
    <div class="layout" :class="{ 'show-editor': showEditor }">
      <aside ref="listPanel" aria-label="我的 Agent 列表" tabindex="-1">
        <div class="list-head"><h3>我的配置</h3><button type="button" :disabled="loading || busy" @click="load()">刷新</button></div>
        <button v-if="editing" type="button" class="mobile-only" data-action="back-editor" @click="focusEditor">返回编辑</button>
        <p v-if="loading" role="status">正在加载…</p>
        <p v-else-if="!agents.length">暂无 Agent，可先新建。</p>
        <button v-for="item in agents" :key="item.id" type="button" class="agent-row"
                :data-agent="item.id" :aria-pressed="selected?.id === item.id" :disabled="busy"
                @click="selectAgent(item.id)">
          <span>{{ item.title }}</span><small v-if="item.status === 'disabled'">已停用</small>
        </button>
        <button v-if="hasMore" type="button" :disabled="loading || busy" @click="load(true)">加载更多</button>
      </aside>
      <form v-if="editing" class="editor" @submit.prevent="save">
        <button type="button" class="mobile-only back-list" data-action="back-list" @click="showList">返回列表</button>
        <h3 ref="editorHeading" tabindex="-1">{{ selected ? `编辑配置 · 版本 ${selected.current_version}` : '新建个人 Agent' }}</h3>
        <p v-if="selected?.status === 'disabled'">该配置已停用，保留历史记录。</p>
        <label for="personal-agent-title">名称</label>
        <input id="personal-agent-title" ref="titleField" v-model="title" required :disabled="busy || runUnresolved || selected?.status === 'disabled'" />
        <label for="personal-agent-prompt">指令</label>
        <textarea id="personal-agent-prompt" v-model="prompt" rows="12" required
                  :disabled="busy || runUnresolved || selected?.status === 'disabled'" placeholder="描述这个 Agent 应遵循的工作方式" />
        <div class="actions">
          <button type="submit" :disabled="busy || runUnresolved || selected?.status === 'disabled'">{{ busy ? '保存中…' : '保存' }}</button>
          <button v-if="selected?.status === 'active'" type="button" data-action="disable" :disabled="busy || runUnresolved" @click="disable">停用</button>
        </div>
        <PersonalPublicationPanel v-if="selected && user.profile?.id" :key="`${user.profile.id}:${selected.id}`"
          :user-id="user.profile.id" :agent-id="selected.id" :version="selected.current_version"
          :available="selected.publication_available && !isDirty && !runUnresolved" />
        <section v-if="selected && (selected.execution_available === true || runPointer || runBlocked)" class="run-panel" aria-label="运行 Agent">
          <h3>运行</h3>
          <p v-if="isDirty">请先保存配置。</p>
          <p v-if="runError" class="error" role="alert">{{ runError }}</p>
          <p v-if="runPointer && !run">待核对请求 · 版本 {{ runPointer.version_number }}</p>
          <p v-if="run" role="status">
            {{ ({ running: '运行中', succeeded: '已完成', awaiting_reconciliation: '用量待核对', result_unavailable: '本次没有可展示的完整结果' })[run.status] }}
            · 版本 {{ run.version_number }} · {{ run.actual_milli === null ? '费用待确认' : `${(run.actual_milli / 1000).toLocaleString('zh-CN')} 积分` }}
          </p>
          <p v-if="run?.status === 'awaiting_reconciliation'">本次费用尚未确认，请等待平台核对。</p>
          <p v-if="run?.status === 'result_unavailable'">重新运行可能再次计费。</p>
          <p v-if="runReason">{{ runReason }}</p>
          <pre v-if="run?.output" class="run-output">{{ run.output }}</pre>
          <label for="personal-agent-run-input">输入内容</label>
          <textarea id="personal-agent-run-input" v-model="runInput" rows="5"
                    :disabled="runBusy || Boolean(runPointer && !runUncertain)"
                    placeholder="输入需要处理的内容" />
          <div class="actions">
            <button v-if="selected.execution_available === true && !runPointer && !runBlocked" type="button" data-action="run"
                    :disabled="busy || runBusy || isDirty || !runInput.trim()" @click="startRun">{{ runBusy ? '提交中…' : '运行' }}</button>
            <button v-if="selected.execution_available === true && runUncertain && runPointer" type="button" data-action="retry-run"
                    :disabled="busy || runBusy || isDirty || !runInput.trim()" @click="startRun">重试同一请求</button>
            <button v-if="runPointer" type="button" :disabled="runBusy" @click="refreshRun">查询状态</button>
            <button v-if="run && ['succeeded', 'result_unavailable'].includes(run.status)" type="button"
                    :disabled="runBusy" @click="newRun">新建运行</button>
          </div>
        </section>
      </form>
      <div v-else class="empty" :role="detailLoading ? 'status' : undefined">{{ detailLoading ? '正在加载…' : '选择一个配置，或新建 Agent。' }}</div>
    </div>
    <AgentRunHistory source="private" />
  </section>
</template>

<style scoped>
.personal-agents { max-width: 1280px; margin: 0 auto; padding: 4px; }
.heading, .list-head, .actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.heading h2, .list-head h3 { margin: 0; }
.notice { color: var(--el-text-color-secondary); font-size: 13px; margin: 8px 0 16px; }
.error { color: var(--el-color-danger); } .message { color: var(--el-color-success); }
.layout { display: grid; grid-template-columns: minmax(200px, 280px) minmax(0, 1fr); gap: 16px; }
aside, .editor, .empty { padding: 16px; min-width: 0; }
aside { border-right: 1px solid var(--el-border-color); }
aside { align-self: start; } .list-head { margin-bottom: 12px; }
.agent-row { display: flex; flex-direction: column; text-align: left; width: 100%; gap: 5px; margin: 6px 0; }
.agent-row small { color: var(--el-text-color-secondary); } .agent-row[aria-pressed="true"] { border-color: var(--el-color-primary); }
.editor { display: flex; flex-direction: column; gap: 10px; } .editor h3 { margin: 0 0 6px; }
input, textarea { width: 100%; box-sizing: border-box; font: inherit; padding: 9px; border: 1px solid var(--el-border-color); border-radius: 6px; }
textarea { resize: vertical; } button { cursor: pointer; padding: 8px 12px; border: 1px solid var(--el-border-color); border-radius: 6px; background: var(--el-bg-color); color: var(--el-text-color-primary); }
.run-panel { display: flex; flex-direction: column; gap: 10px; border-top: 1px solid var(--el-border-color); padding-top: 16px; margin-top: 8px; }
.run-panel p { margin: 0; } .run-output { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; margin: 0; }
button:disabled { cursor: not-allowed; opacity: .55; } .actions { justify-content: flex-start; }
button:focus-visible, input:focus-visible, textarea:focus-visible, aside:focus-visible, h3:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }
.mobile-only { display: none; }
@media (max-width: 760px) {
  .heading { align-items: flex-start; flex-direction: column; }
  .layout { grid-template-columns: 1fr; }
  aside { border-right: 0; }
  .layout.show-editor aside, .layout:not(.show-editor) .editor { display: none; }
  .mobile-only { display: inline-block; }
  .back-list { align-self: flex-start; }
  button { min-height: 44px; }
}
</style>
