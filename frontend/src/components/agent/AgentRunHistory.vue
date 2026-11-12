<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { useUserStore } from '@/stores/user'
import { getToken } from '@/utils/token'
import { getAgentRun, listAgentRuns, type AgentRunSource, type AgentRunSummary } from '@/api/agentRunHistory'
import type { PersonalAgentRun } from '@/api/personalAgents'
import type { PublicAgentRun } from '@/api/personalPublications'
import type { EnterpriseAssetRun } from '@/api/enterpriseAssetRuns'

const props = defineProps<{ source: AgentRunSource }>()
const user = useUserStore()
const rows = ref<AgentRunSummary[]>([]), detail = ref<PersonalAgentRun | PublicAgentRun | EnterpriseAssetRun | null>(null)
const opened = ref(false), loading = ref(false), more = ref(false), error = ref('')
const detailTitle = ref('')
const retryRow = ref<AgentRunSummary | null>(null)
let generation = 0
const labels = { running: '运行中', succeeded: '已完成', awaiting_reconciliation: '用量待核对', result_unavailable: '结果不可恢复' }
function current(seq: number, token: string, account: number) {
  return seq === generation && token === getToken() && token === user.token && account === user.profile?.id
}
async function load(append = false) {
  const token = getToken(), account = user.profile?.id, seq = generation
  if (!token || token !== user.token || !account || loading.value) return
  loading.value = true; error.value = ''; retryRow.value = null
  try {
    const page = await listAgentRuns(props.source, token, append ? rows.value.at(-1)?.id : undefined)
    if (current(seq, token, account)) { rows.value = append ? [...rows.value, ...page] : page; more.value = page.length === 20 }
  } catch { if (current(seq, token, account)) error.value = '记录加载失败，请重试。' }
  finally { if (current(seq, token, account)) loading.value = false }
}
async function select(row: AgentRunSummary) {
  const token = getToken(), account = user.profile?.id, seq = ++generation
  if (!token || token !== user.token || !account) return
  detail.value = null; detailTitle.value = ''; loading.value = true; error.value = ''; retryRow.value = null
  try {
    const result = await getAgentRun(row, token)
    if (result.id !== row.id ||
        ('agent_id' in result ? result.agent_id : null) !== row.agent_id ||
        ('publication_id' in result ? result.publication_id : null) !== row.publication_id ||
        ('enterprise_publication_id' in result ? result.enterprise_publication_id : null) !== (row.enterprise_publication_id ?? null)) throw new Error('run mismatch')
    if (current(seq, token, account)) { detail.value = result; detailTitle.value = row.title }
  } catch { if (current(seq, token, account)) { error.value = '结果加载失败，请重试。'; retryRow.value = row } }
  finally { if (current(seq, token, account)) loading.value = false }
}
function toggle(event: Event) {
  opened.value = (event.target as HTMLDetailsElement).open
  if (opened.value) void load()
}
watch(() => [user.profile?.id, user.token, props.source] as const, () => {
  ++generation; rows.value = []; detail.value = null; detailTitle.value = ''; retryRow.value = null; loading.value = false; more.value = false; error.value = ''
  if (opened.value) void load()
}, { flush: 'sync' })
onBeforeUnmount(() => { ++generation })
</script>

<template>
  <details class="history" @toggle="toggle">
    <summary>运行记录</summary>
    <p v-if="error" role="alert">{{ error }} <button type="button" @click="retryRow ? select(retryRow) : load()">重试</button></p>
    <p v-if="loading" role="status">正在加载…</p>
    <p v-else-if="!rows.length && !error">暂无记录</p>
    <button v-for="row in rows" :key="row.id" type="button" class="history-row" :data-run="row.id" :disabled="loading" @click="select(row)">
      <span>{{ row.title }} · v{{ row.version_number }}</span>
      <span>{{ labels[row.status] }} · {{ row.actual_milli === null ? '费用待确认' : `${(row.actual_milli / 1000).toLocaleString('zh-CN')} 积分` }}</span>
      <time>{{ new Date(row.created_at).toLocaleString('zh-CN') }}</time>
    </button>
    <button v-if="more" type="button" data-action="more" :disabled="loading" @click="load(true)">加载更多</button>
    <div v-if="detail" class="history-detail" aria-label="运行结果">
      <h3>{{ detailTitle }}</h3>
      <p>{{ labels[detail.status] }} · {{ detail.actual_milli === null ? '费用待确认' : `${(detail.actual_milli / 1000).toLocaleString('zh-CN')} 积分` }}</p>
      <pre v-if="detail.output">{{ detail.output }}</pre>
    </div>
  </details>
</template>

<style scoped>
.history { margin-top: 1.5rem; border-top: 1px solid var(--border-color, #e5e7eb); padding-top: 1rem; }
summary { cursor: pointer; font-weight: 600; }
.history-row { display: flex; flex-wrap: wrap; justify-content: space-between; gap: .5rem; width: 100%; text-align: left; margin-top: .5rem; }
.history-row time { color: var(--text-secondary, #666); }
.history-detail pre { white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
