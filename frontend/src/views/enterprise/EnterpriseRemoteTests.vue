<template>
  <section class="remote-workbench" aria-labelledby="enterprise-remote-title">
    <header class="workbench-header">
      <div>
        <h2 id="enterprise-remote-title">企业远测申请与审批</h2>
        <p>申请固定到有执行权限的企业项目。企业 owner/admin 可审批本人申请；小组长仅能审批本组其他成员。批准不会探测目标，须由申请人主动启动。</p>
      </div>
      <button type="button" :disabled="loading || busy" @click="load">{{ loading ? '刷新中…' : '刷新' }}</button>
    </header>

    <p v-if="error" class="remote-error" role="alert">{{ error }}</p>
    <p v-if="uncertain" class="remote-warning" role="alert">
      上次操作的结果尚未确认。请刷新列表核对服务器状态；刷新成功前不能重复提交、审批、撤销或启动。
    </p>

    <form class="request-card" @submit.prevent="submit">
      <div class="section-heading">
        <div><h3>提交远测申请</h3><p>只接受 HTTPS 域名根地址，不支持 IP、端口、路径、查询参数或片段。</p></div>
      </div>
      <p v-if="!executableProjects.length && !loading" class="muted">
        当前没有可执行的企业项目；需要项目成员权限后才能申请。
      </p>
      <div class="request-grid">
        <label class="field project-field" for="remote-project">
          <span>企业项目</span>
          <select id="remote-project" v-model.number="form.projectId" :disabled="disabled || !executableProjects.length">
            <option :value="0">选择有执行权限的项目</option>
            <option v-for="project in executableProjects" :key="project.project_id" :value="project.project_id">
              {{ project.project_name }} · {{ project.group_name }}
            </option>
          </select>
        </label>
        <label class="field target-field" for="remote-target-url">
          <span>远测目标</span>
          <input id="remote-target-url" v-model.trim="form.targetUrl" autocomplete="url" maxlength="512"
            placeholder="https://example.com/" :disabled="disabled" />
        </label>
        <label class="field" for="remote-authorization-basis">
          <span>资产授权依据</span>
          <textarea id="remote-authorization-basis" v-model.trim="form.authorizationBasis" rows="3" maxlength="1000"
            placeholder="说明你或企业对该站点的所有权或书面授权来源（至少 12 个字符）" :disabled="disabled" />
        </label>
        <label class="field" for="remote-scope-notes">
          <span>范围和排除项</span>
          <textarea id="remote-scope-notes" v-model.trim="form.scopeNotes" rows="3" maxlength="2000"
            placeholder="说明本次检查范围和明确排除项" :disabled="disabled" />
        </label>
        <label class="field window-field" for="remote-window-hours">
          <span>授权时间窗</span>
          <select id="remote-window-hours" v-model.number="form.windowHours" :disabled="disabled">
            <option v-for="hours in windowOptions" :key="hours" :value="hours">{{ hours }} 小时</option>
          </select>
        </label>
      </div>
      <label class="acknowledgement">
        <input v-model="form.ackRules" type="checkbox" :disabled="disabled" />
        <span>我已确认目标授权依据真实有效，并已阅读以上企业远测规则。</span>
      </label>
      <div class="form-actions">
        <button type="submit" class="primary" :disabled="disabled || !canSubmit">
          {{ busy ? '正在提交…' : '提交审批申请' }}
        </button>
      </div>
    </form>

    <section class="request-list" aria-labelledby="remote-list-title">
      <header class="section-heading list-heading">
        <div><h3 id="remote-list-title">申请记录与待审批</h3><p>列表由服务端按企业角色和当前小组权限过滤。</p></div>
        <span class="count">{{ total }} 条</span>
      </header>
      <p v-if="loading && !items.length" class="remote-state" role="status">正在读取企业远测申请…</p>
      <div v-else-if="!items.length && !error" class="remote-empty">
        <span aria-hidden="true">⌕</span><h4>暂时没有远测申请</h4><p>提交申请后，你可以在这里查看审批和启动状态。</p>
      </div>
      <ol v-else class="request-list-items">
        <li v-for="item in items" :key="item.id" class="request-item">
          <div class="request-topline">
            <span class="status-pill" :class="`status-${item.status}`">{{ statusLabel(item.status) }}</span>
            <span>{{ groupName(item.group_id) }} · {{ projectName(item.project_id) }}</span>
            <time v-if="item.create_time" :datetime="item.create_time">{{ formatTime(item.create_time) }}</time>
          </div>
          <h4>{{ item.target_host }} <small>· 申请 #{{ item.id }} · 申请人 {{ item.owner_id }}</small></h4>
          <dl class="scope-details">
            <div><dt>固定目标</dt><dd>{{ item.target_url }}</dd></div>
            <div><dt>授权依据</dt><dd>{{ item.authorization_basis }}</dd></div>
            <div><dt>检查范围</dt><dd>{{ item.scope_notes || '未填写额外排除项' }}</dd></div>
            <div><dt>授权时长</dt><dd>{{ item.requested_window_hours }} 小时</dd></div>
            <div v-if="item.decided_by"><dt>处理人</dt><dd>成员 {{ item.decided_by }}<span v-if="item.decision_reason"> · {{ item.decision_reason }}</span></dd></div>
          </dl>
          <p v-if="item.status === 'approved' && item.owner_id === currentUserId && !item.execution_available"
            class="muted">执行入口未开放或授权时间窗已结束，请刷新核对申请状态。</p>
          <div class="request-actions">
            <button v-if="canApprove(item)" type="button" class="approve" :disabled="disabled" @click="decide(item, 'approved')">批准范围</button>
            <button v-if="canApprove(item)" type="button" class="reject" :disabled="disabled" @click="decide(item, 'rejected')">拒绝申请</button>
            <button v-if="canStart(item)" type="button" class="primary" :disabled="disabled" @click="start(item)">启动只读评估</button>
            <button v-if="canCancel(item)" type="button" :disabled="disabled" @click="cancel(item)">撤回申请 / 授权</button>
          </div>
        </li>
      </ol>
      <div v-if="total > pageSize" class="pagination">
        <button type="button" :disabled="disabled || page === 0" @click="changePage(page - 1)">上一页</button>
        <span>第 {{ page + 1 }} 页</span>
        <button type="button" :disabled="disabled || (page + 1) * pageSize >= total" @click="changePage(page + 1)">下一页</button>
      </div>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import {
  cancelEnterpriseRemoteApproval, decideEnterpriseRemoteApproval, listEnterpriseProjects,
  listEnterpriseRemoteApprovals, startEnterpriseRemoteApproval, submitEnterpriseRemoteApproval,
  type EnterpriseGroup, type EnterpriseProjectSummary, type EnterpriseRemoteApproval,
} from '@/api/enterprise'
import { getToken } from '@/utils/token'
import { isAuthorizableRemoteOrigin, toRemoteTargetOrigin } from '@/utils/remoteTarget'

const props = defineProps<{
  enterpriseId: number
  currentUserId: number
  role: 'owner' | 'admin' | 'member'
  groups: EnterpriseGroup[]
}>()
const emit = defineEmits<{ 'access-lost': [] }>()
const router = useRouter()
const items = ref<EnterpriseRemoteApproval[]>([])
const projects = ref<EnterpriseProjectSummary[]>([])
const total = ref(0)
const page = ref(0)
const loading = ref(false)
const busy = ref(false)
const uncertain = ref(false)
const error = ref('')
const pageSize = 20
const windowOptions = [1, 2, 4, 8, 12, 24]
const form = reactive({ projectId: 0, targetUrl: '', authorizationBasis: '', scopeNotes: '', windowHours: 4, ackRules: false })
const executableProjects = computed(() => projects.value.filter(project => project.role !== 'viewer'))
const selectedProject = computed(() => executableProjects.value.find(project => project.project_id === form.projectId) || null)
const canSubmit = computed(() => Boolean(
  selectedProject.value && form.targetUrl.trim() && form.authorizationBasis.trim().length >= 12 && form.ackRules,
))
const disabled = computed(() => loading.value || busy.value || uncertain.value)
let generation = 0
let operationGeneration = 0

function errorCode(cause: unknown): number {
  if (!cause || typeof cause !== 'object' || !('code' in cause)) return 0
  return Number((cause as { code?: unknown }).code) || 0
}
function isDenied(cause: unknown): boolean {
  const code = errorCode(cause)
  return (code >= 40300 && code < 40500) || code === 403 || code === 404
}
function isDefiniteClientRejection(cause: unknown): boolean {
  const code = errorCode(cause)
  const status = cause && typeof cause === 'object' && 'status' in cause
    ? Number((cause as { status?: unknown }).status) : 0
  return (code >= 400 && code < 500) || (code >= 40000 && code < 50000) || (status >= 400 && status < 500)
}
function clearData() { items.value = []; projects.value = []; total.value = 0 }
function clearForm() {
  form.projectId = 0; form.targetUrl = ''; form.authorizationBasis = ''; form.scopeNotes = ''; form.ackRules = false
}
function groupName(groupId: number) {
  return props.groups.find(group => group.id === groupId)?.name || `小组 ${groupId}`
}
function projectName(projectId: number | null) {
  return projectId ? projects.value.find(project => project.project_id === projectId)?.project_name || `项目 ${projectId}` : '无有效项目归属'
}
function canApprove(item: EnterpriseRemoteApproval) {
  if (item.status !== 'pending') return false
  if (props.role === 'owner' || props.role === 'admin') return true
  return item.owner_id !== props.currentUserId
    && props.groups.some(group => group.id === item.group_id && group.my_role === 'leader')
}
function canStart(item: EnterpriseRemoteApproval) {
  return item.owner_id === props.currentUserId && item.status === 'approved' && item.execution_available
}
function canCancel(item: EnterpriseRemoteApproval) {
  return item.owner_id === props.currentUserId && ['pending', 'approved'].includes(item.status)
}
function statusLabel(status: string) {
  return ({ pending: '待审批', approved: '已批准 · 待申请人启动', rejected: '已拒绝', cancelled: '已撤回' } as Record<string, string>)[status] || status
}
function formatTime(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}
function markUnknown(message: string) {
  uncertain.value = true
  error.value = message
}
async function handleOperationFailure(cause: unknown, unknownMessage: string, rejectedMessage: string) {
  if (isDenied(cause)) {
    clearData(); clearForm()
    error.value = '企业或小组权限已变化，远测内容已清除。'
    emit('access-lost')
    return
  }
  if (isDefiniteClientRejection(cause)) {
    await load()
    if (!error.value) error.value = rejectedMessage
    return
  }
  markUnknown(unknownMessage)
}
async function load() {
  const current = ++generation
  loading.value = true
  error.value = ''
  try {
    const [projectRows, requestPage] = await Promise.all([
      listEnterpriseProjects(props.enterpriseId),
      listEnterpriseRemoteApprovals(props.enterpriseId, pageSize, page.value * pageSize),
    ])
    if (current !== generation) return
    projects.value = projectRows
    items.value = requestPage.items || []
    total.value = requestPage.total || 0
    uncertain.value = false
    if (!selectedProject.value && executableProjects.value.length) form.projectId = executableProjects.value[0].project_id
  } catch (cause) {
    if (current !== generation) return
    if (isDenied(cause)) {
      clearData(); clearForm()
      error.value = '企业或小组权限已变化，申请内容已清除。'
      emit('access-lost')
    } else error.value = '企业远测信息加载失败，请刷新重试。'
  } finally {
    if (current === generation) loading.value = false
  }
}
function normalizeTarget(raw: string): string | null {
  try {
    const parsed = new URL(raw.trim())
    if (parsed.username || parsed.password || parsed.pathname !== '/' || parsed.search || parsed.hash) return null
  } catch { return null }
  const origin = toRemoteTargetOrigin(raw)
  return isAuthorizableRemoteOrigin(origin) ? origin : null
}
async function submit() {
  if (!canSubmit.value || !selectedProject.value || disabled.value) return
  const target = normalizeTarget(form.targetUrl)
  if (!target) {
    error.value = '目标必须是 HTTPS 域名根地址，不能包含凭据、端口、路径、查询参数或片段。'
    return
  }
  if (form.authorizationBasis.trim().length > 1000 || form.scopeNotes.trim().length > 2000) {
    error.value = '授权依据或范围说明超过长度限制。'
    return
  }
  const current = generation
  const operation = ++operationGeneration
  busy.value = true
  error.value = ''
  try {
    await submitEnterpriseRemoteApproval(props.enterpriseId, {
      group_id: selectedProject.value.group_id,
      project_id: selectedProject.value.project_id,
      target_url: target,
      authorization_basis: form.authorizationBasis.trim(),
      scope_notes: form.scopeNotes.trim(),
      requested_window_hours: form.windowHours,
      ack_rules: true,
    }, getToken())
    if (current !== generation) return
    form.targetUrl = ''; form.authorizationBasis = ''; form.scopeNotes = ''; form.ackRules = false
    page.value = 0
    await load()
  } catch (cause) {
    if (current === generation) await handleOperationFailure(
      cause, '提交结果未知。请刷新列表确认是否已生成申请，不要重复提交。',
      '申请未创建；企业额度、项目权限或字段状态已变化，请检查后再提交。',
    )
  } finally {
    if (operation === operationGeneration) busy.value = false
  }
}
async function decide(item: EnterpriseRemoteApproval, decision: 'approved' | 'rejected') {
  if (!canApprove(item) || disabled.value) return
  const verb = decision === 'approved' ? '批准' : '拒绝'
  if (!window.confirm(`${verb}对 ${item.target_url} 的固定范围申请？批准本身不会探测目标。`)) return
  const reason = window.prompt(decision === 'approved' ? '审批说明（必填）' : '拒绝原因（必填）',
    decision === 'approved' ? '已核验授权依据、项目和范围' : '')
  if (!reason?.trim()) return
  const current = generation
  const operation = ++operationGeneration
  busy.value = true; error.value = ''
  try {
    await decideEnterpriseRemoteApproval(props.enterpriseId, item.id, decision, reason.trim(), getToken())
    if (current === generation) await load()
  } catch (cause) {
    if (current === generation) await handleOperationFailure(
      cause, '审批结果未知。请刷新核对当前状态，不要直接重复决定。',
      '审批未执行；申请状态或当前审批权限已变化，列表已刷新。',
    )
  } finally {
    if (operation === operationGeneration) busy.value = false
  }
}
async function cancel(item: EnterpriseRemoteApproval) {
  if (!canCancel(item) || disabled.value) return
  if (!window.confirm(`撤回对 ${item.target_url} 的申请/未启动授权？已启动的任务不能撤回。`)) return
  const current = generation
  const operation = ++operationGeneration
  busy.value = true; error.value = ''
  try {
    await cancelEnterpriseRemoteApproval(props.enterpriseId, item.id, getToken())
    if (current === generation) await load()
  } catch (cause) {
    if (current === generation) await handleOperationFailure(
      cause, '撤回结果未知。请刷新核对申请与任务状态。',
      '撤回未执行；任务可能已启动或申请状态已变化，列表已刷新。',
    )
  } finally {
    if (operation === operationGeneration) busy.value = false
  }
}
async function start(item: EnterpriseRemoteApproval) {
  if (!canStart(item) || disabled.value) return
  if (!window.confirm(`现在启动 ${item.target_url} 的受限只读评估？确认后会向该域名发送已授权的检查请求。`)) return
  const current = generation
  const operation = ++operationGeneration
  busy.value = true; error.value = ''
  try {
    const result = await startEnterpriseRemoteApproval(props.enterpriseId, item.id, getToken())
    if (current !== generation) return
    const publicId = result.execution?.public_id
    if (!publicId) {
      markUnknown('服务端未返回执行任务编号，请刷新申请与任务状态。')
      return
    }
    await router.push(`/remote-pentests/${encodeURIComponent(publicId)}`)
  } catch (cause) {
    if (current === generation) await handleOperationFailure(
      cause, '启动结果未知。请刷新核对任务状态，避免重复启动。',
      '启动未执行；授权窗口、企业状态或任务状态已变化，列表已刷新。',
    )
  } finally {
    if (operation === operationGeneration) busy.value = false
  }
}
async function changePage(next: number) {
  if (next < 0 || next * pageSize >= total.value || disabled.value) return
  page.value = next
  await load()
}
watch(() => [props.enterpriseId, props.currentUserId, props.role] as const, () => {
  ++generation; ++operationGeneration; clearData(); page.value = 0; loading.value = false; busy.value = false
  uncertain.value = false; error.value = ''
  clearForm()
  void load()
}, { immediate: true, flush: 'sync' })
watch(
  () => props.groups
    .map(group => `${group.id}:${group.status}:${group.my_role || ''}`)
    .sort()
    .join('|'),
  () => {
    // A group membership or leader-role change can invalidate the rows already
    // rendered here. Hide those rows before asking the server for the new scope.
    ++generation; ++operationGeneration; clearData(); clearForm(); page.value = 0
    loading.value = false; busy.value = false; uncertain.value = false; error.value = ''
    void load()
  },
)
onMounted(() => { if (generation === 0) void load() })
onBeforeUnmount(() => { ++generation })
</script>

<style scoped>
.remote-workbench { min-height: 360px; padding: 22px; border: 1px solid #e4e7ed; border-radius: 14px; background: #fff; color: #252a3a; }
.workbench-header, .section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.workbench-header { padding-bottom: 16px; border-bottom: 1px solid #eceef2; }
.workbench-header h2, .section-heading h3 { margin: 0; font-size: 19px; }
.workbench-header p, .section-heading p { margin: 7px 0 0; color: #777f91; font-size: 13px; line-height: 1.6; }
button, input, select, textarea { font: inherit; }
button { min-height: 36px; padding: 7px 12px; border: 1px solid #d9ddeb; border-radius: 8px; background: #fff; color: inherit; cursor: pointer; }
button:disabled { opacity: .55; cursor: default; }
.primary { border-color: #315fc3; background: #315fc3; color: #fff; }
.request-card, .request-list { margin-top: 18px; padding: 18px; border: 1px solid #e7e9ef; border-radius: 12px; background: #fbfcfe; }
.section-heading h3 { font-size: 16px; }.request-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px 18px; margin-top: 16px; }
.field { display: grid; gap: 6px; min-width: 0; color: #4d5569; font-size: 13px; font-weight: 650; }
.field input, .field select, .field textarea { width: 100%; min-width: 0; padding: 9px 10px; border: 1px solid #d9ddeb; border-radius: 8px; background: #fff; color: #252a3a; font-weight: 400; }
.target-field, .project-field { grid-column: 1 / -1; }.field textarea { resize: vertical; line-height: 1.55; }
.acknowledgement { display: flex; align-items: flex-start; gap: 9px; margin-top: 15px; color: #606a7d; font-size: 13px; line-height: 1.6; }
.acknowledgement input { margin-top: 4px; }.form-actions, .request-actions, .pagination { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.form-actions { margin-top: 14px; }.request-list { background: #fff; }.list-heading { align-items: center; padding-bottom: 14px; border-bottom: 1px solid #eceef2; }.count { color: #7a8292; font-size: 13px; }
.remote-error, .remote-warning, .remote-state { margin: 14px 0; padding: 12px 14px; border-radius: 9px; font-size: 13px; line-height: 1.6; }
.remote-error { background: #fff2f2; color: #a23434; }.remote-warning { background: #fff8e9; color: #8e5a15; }.remote-state, .muted { color: #69718a; }
.remote-empty { display: grid; justify-items: center; padding: 48px 20px; text-align: center; color: #69718a; }.remote-empty > span { font-size: 24px; }.remote-empty h4 { margin: 8px 0 0; color: #343a4b; }.remote-empty p { margin: 6px 0 0; font-size: 13px; }
.request-list-items { display: grid; gap: 12px; margin: 16px 0 0; padding: 0; list-style: none; }.request-item { min-width: 0; padding: 16px; border: 1px solid #e7e9ef; border-radius: 10px; }
.request-topline { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; color: #71798a; font-size: 12px; }.request-topline time { margin-left: auto; }
.status-pill { padding: 3px 8px; border-radius: 999px; background: #edf1f7; color: #566074; }.status-pending { background: #fff5df; color: #8e5f12; }.status-approved { background: #e9f7ef; color: #286843; }.status-rejected, .status-cancelled { background: #f0f1f4; color: #656d7c; }
.request-item h4 { margin: 12px 0 8px; overflow-wrap: anywhere; }.request-item h4 small { color: #808797; font-size: 12px; font-weight: 400; }
.scope-details { display: grid; gap: 8px; margin: 0; }.scope-details > div { display: grid; grid-template-columns: 86px minmax(0, 1fr); gap: 10px; }.scope-details dt { color: #81889a; font-size: 12px; }.scope-details dd { min-width: 0; margin: 0; color: #41495c; font-size: 13px; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }
.request-actions { margin-top: 14px; }.request-actions .approve { border-color: #c6ddcf; background: #edf8f1; color: #286843; }.request-actions .reject { border-color: #ead0d0; background: #fff4f4; color: #9b4a4a; }
.pagination { align-items: center; margin-top: 16px; color: #6c7485; font-size: 13px; }
@media (max-width: 680px) { .remote-workbench { padding: 15px; }.workbench-header { align-items: flex-start; }.request-card, .request-list { padding: 14px; }.request-grid { grid-template-columns: 1fr; }.target-field, .project-field { grid-column: 1; }.request-topline time { width: 100%; margin-left: 0; }.scope-details > div { grid-template-columns: 68px minmax(0, 1fr); } }
</style>
