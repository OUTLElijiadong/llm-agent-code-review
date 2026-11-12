<template>
  <section class="assets">
    <div class="heading"><div><h2>企业 Agent 与 Skill</h2><p class="muted">目录不提供直接运行。配置兼容版本可提交给企业小菱尝试调用；实际运行仍要求企业权限、计量、订阅、额度和平台模型配置均可用。</p></div><div class="actions">
      <button type="button" :disabled="busy || loading || auxiliaryLoading" @click="refresh">刷新列表</button>
      <button v-if="view === 'drafts'" type="button" data-action="new-asset" :disabled="busy" @click="newDraft">新建草稿</button>
    </div></div>
    <nav class="asset-tabs" aria-label="Agent 与 Skill 页面">
      <button type="button" :aria-pressed="view === 'drafts'" @click="selectView('drafts')">我的草稿</button>
      <button type="button" :aria-pressed="view === 'catalog'" @click="selectView('catalog')">企业目录</button>
      <button type="button" :aria-pressed="view === 'requests'" @click="selectView('requests')">发布申请</button>
    </nav>
    <p class="notice">草稿仅作者可见。企业内发布由企业 owner/admin 审批；全局发布由平台最高管理员审批。目录逐项标出运行配置是否兼容。兼容只代表配置格式和大小符合受限运行器要求；调用仍受企业状态、权限、计量开关、订阅、额度和平台模型配置限制，成功调用按企业积分计费。</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p><p v-if="message" role="status">{{ message }}</p>
    <div v-if="view === 'drafts'" class="columns">
      <aside><p v-if="loading && !drafts.length">正在加载草稿…</p><p v-else-if="!drafts.length">暂无草稿。</p>
        <button v-for="draft in drafts" :key="draft.id" :data-draft="draft.id" class="draft"
          type="button" :disabled="busy" :aria-pressed="selected?.id === draft.id" @click="open(draft.id)">
          {{ draft.title }} · {{ draft.kind === 'agent' ? 'Agent' : 'Skill' }} · 版本 {{ draft.current_version }}
        </button>
        <button v-if="hasMore" type="button" :disabled="busy || loading" @click="load(true)">加载更多草稿</button>
      </aside>
      <div v-if="editing" class="editor">
        <h3>{{ selected ? `编辑草稿 · 版本 ${selected.current_version}` : '新建草稿' }}</h3>
        <form @submit.prevent="save">
          <label for="asset-kind">类型</label><select id="asset-kind" v-model="kind" :disabled="Boolean(selected) || busy">
            <option value="agent">Agent</option><option value="skill">Skill</option></select>
          <label for="asset-title">草稿名称</label><input id="asset-title" v-model.trim="title" maxlength="120" :disabled="busy" />
          <label for="asset-definition">配置（JSON 对象）</label>
          <textarea id="asset-definition" v-model="definitionText" rows="13" spellcheck="false" :disabled="busy" />
          <div class="actions"><button type="submit" :disabled="busy || !title.trim()">{{ busy ? '保存中…' : '保存草稿' }}</button>
            <button v-if="selected" type="button" :disabled="busy" @click="open(selected.id)">重新加载当前版本</button>
            <button v-if="selected" type="button" :disabled="busy" @click="requestPublication('enterprise')">申请企业内发布</button>
            <button v-if="selected" type="button" :disabled="busy" @click="requestPublication('global')">申请全局发布</button>
            <button v-if="selected" type="button" :disabled="busy" @click="remove">删除草稿</button></div>
          <p v-if="selected?.published_scopes?.length" class="muted">当前版本发布范围：{{ scopeLabels(selected.published_scopes) }}</p>
          <p class="muted">执行状态：关闭。发布的版本固定为当前保存版本；修改后需重新申请。</p>
        </form>
      </div>
      <div v-else class="empty">选择已有草稿，或新建一个草稿。</div>
    </div>
    <section v-else-if="view === 'catalog'" class="catalog" aria-label="企业资产目录">
      <p v-if="auxiliaryLoading && !catalog.length" role="status">正在加载企业目录…</p>
      <p v-else-if="!catalog.length" class="empty">企业内暂时没有已发布 Agent 或 Skill。</p>
      <article v-for="item in catalog" :key="item.id" class="publication-card">
        <div class="publication-title"><strong>{{ item.title }}</strong><span>{{ kindLabel(item.kind) }} · v{{ item.version_number }}</span></div>
        <p class="muted">企业内可见 · 目录不能直接运行；{{ item.xiaoling_runtime_config_compatible ? '运行配置格式与大小兼容；实际调用仍受企业权限、计量、订阅、额度和模型配置限制' : '当前小菱运行器不兼容此版本配置' }} · SHA-256 {{ item.content_sha256.slice(0, 12) }}…</p>
        <pre>{{ JSON.stringify(item.definition, null, 2) }}</pre>
        <button v-if="item.can_revoke" type="button" :disabled="busy"
          @click="revoke(item, 'enterprise')">撤销企业发布</button>
      </article>
    </section>
    <section v-else class="requests" aria-label="发布申请记录">
      <h3>我提交的申请</h3>
      <p v-if="auxiliaryLoading && !myPublications.length" role="status">正在加载发布申请…</p>
      <p v-else-if="!myPublications.length" class="empty">暂无发布申请。</p>
      <article v-for="item in myPublications" :key="item.id" class="publication-card">
        <div class="publication-title"><strong>{{ item.title }}</strong><span>{{ kindLabel(item.kind) }} · {{ scopeLabel(item.scope) }} · v{{ item.version_number }}</span></div>
        <p>状态：{{ statusLabel(item.status) }} · 内容摘要 {{ item.content_sha256.slice(0, 12) }}…</p>
        <p v-if="item.decision_reason" class="muted">审批说明：{{ item.decision_reason }}</p>
        <button v-if="item.status === 'pending'" type="button" :disabled="busy" @click="withdraw(item)">撤回申请</button>
      </article>
      <template v-if="canManage">
        <h3>待企业审批</h3>
        <label for="enterprise-publication-reason">审批说明</label>
        <input id="enterprise-publication-reason" v-model="decisionReason" maxlength="500" placeholder="选填，记录审批原因" />
        <p v-if="auxiliaryLoading && !pendingPublications.length" role="status">正在加载待审批申请…</p>
        <p v-else-if="!pendingPublications.length" class="empty">暂无待审批的企业内发布。</p>
        <article v-for="item in pendingPublications" :key="item.id" class="publication-card">
          <div class="publication-title"><strong>{{ item.title }}</strong><span>{{ kindLabel(item.kind) }} · v{{ item.version_number }}</span></div>
          <p>申请人 {{ item.requested_by }} · 摘要 {{ item.content_sha256 }}</p>
          <pre>{{ JSON.stringify(item.definition, null, 2) }}</pre>
          <div class="actions">
            <button type="button" :disabled="busy" @click="decide(item, 'approve')">批准企业内发布</button>
            <button type="button" :disabled="busy" @click="decide(item, 'reject')">拒绝</button>
          </div>
        </article>
      </template>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { getToken } from '@/utils/token'
import { createEnterpriseAssetDraft, deleteEnterpriseAssetDraft, getEnterpriseAssetDraft,
  decideEnterpriseAssetPublication, listEnterpriseAssetCatalog, listEnterpriseAssetDrafts,
  listMyEnterpriseAssetPublications, listPendingEnterpriseAssetPublications,
  requestEnterpriseAssetPublication, reviseEnterpriseAssetDraft, revokeEnterpriseAssetPublication,
  withdrawEnterpriseAssetPublication, type EnterpriseAssetCatalogItem, type EnterpriseAssetDraft,
  type EnterpriseAssetPublication }
  from '@/api/enterpriseAssets'

const props = withDefaults(defineProps<{ enterpriseId: number; currentUserId: number; role?: string }>(), { role: 'member' })
const emit = defineEmits<{ 'access-lost': [] }>()
const drafts = ref<EnterpriseAssetDraft[]>([]), selected = ref<EnterpriseAssetDraft | null>(null)
const editing = ref(false), kind = ref<'agent' | 'skill'>('agent'), title = ref(''), definitionText = ref('{}')
const busy = ref(false), loading = ref(false), hasMore = ref(false), error = ref(''), message = ref('')
type AssetView = 'drafts' | 'catalog' | 'requests'
const view = ref<AssetView>('drafts')
const catalog = ref<EnterpriseAssetCatalogItem[]>([])
const myPublications = ref<EnterpriseAssetPublication[]>([])
const pendingPublications = ref<EnterpriseAssetPublication[]>([])
const decisionReason = ref('')
const auxiliaryLoading = ref(false)
const canManage = computed(() => ['owner', 'admin'].includes(props.role))
let scopeGeneration = 0, listGeneration = 0, selectionGeneration = 0, auxiliaryGeneration = 0
let savedEditor = '', scopeToken: string | null = null
function editorSnapshot() { return JSON.stringify([kind.value, title.value, definitionText.value]) }
const dirty = computed(() => editing.value && editorSnapshot() !== savedEditor)
function mayLeave() {
  return scopeToken !== getToken() || !dirty.value || window.confirm('当前修改尚未保存，确定放弃并继续吗？')
}
defineExpose({ mayLeave })

function clear() {
  drafts.value = []; selected.value = null; editing.value = false; kind.value = 'agent'
  title.value = ''; definitionText.value = '{}'; busy.value = false; loading.value = false
  hasMore.value = false; error.value = ''; message.value = ''; view.value = 'drafts'
  catalog.value = []; myPublications.value = []; pendingPublications.value = []
  decisionReason.value = ''; auxiliaryLoading.value = false
  savedEditor = ''
}
function isDenied(cause: unknown) {
  const code = typeof cause === 'object' && cause !== null && 'code' in cause ? Number(cause.code) : 0
  return code === 403 || (code >= 40300 && code < 40400)
}
function codeOf(cause: unknown) {
  return typeof cause === 'object' && cause !== null && 'code' in cause ? Number(cause.code) : 0
}
function denied() {
  ++scopeGeneration; ++listGeneration; ++selectionGeneration; clear(); emit('access-lost')
}
async function load(more = false) {
  const scope = scopeGeneration, request = ++listGeneration, id = props.enterpriseId, token = getToken()
  loading.value = true; error.value = ''
  try {
    const rows = await listEnterpriseAssetDrafts(id, more ? drafts.value.length : 0, token)
    if (scope !== scopeGeneration || request !== listGeneration || token !== getToken()) return
    if (more) {
      const seen = new Set(drafts.value.map(item => item.id))
      drafts.value.push(...rows.filter(item => !seen.has(item.id)))
    } else drafts.value = rows
    hasMore.value = rows.length === 100
  } catch (cause) {
    if (scope !== scopeGeneration || request !== listGeneration || token !== getToken()) return
    if (isDenied(cause)) denied()
    else error.value = '草稿列表加载失败，请重试。'
  } finally { if (scope === scopeGeneration && request === listGeneration) loading.value = false }
}
function kindLabel(value: string) { return value === 'agent' ? 'Agent' : 'Skill' }
function scopeLabel(value: string) { return value === 'global' ? '全局' : '企业内' }
function scopeLabels(values: string[]) { return values.map(scopeLabel).join('、') }
function statusLabel(value: string) {
  return ({ pending: '待审批', approved: '已发布', rejected: '已拒绝', withdrawn: '已撤回', revoked: '已撤销' } as Record<string, string>)[value] || value
}
function selectView(next: AssetView) {
  view.value = next; error.value = ''; message.value = ''
  if (next === 'catalog' || next === 'requests') void loadAuxiliary(next)
}
async function loadAuxiliary(target: 'catalog' | 'requests' = view.value as 'catalog' | 'requests') {
  const scope = scopeGeneration, request = ++auxiliaryGeneration, enterpriseId = props.enterpriseId, token = getToken()
  auxiliaryLoading.value = true; error.value = ''
  try {
    if (target === 'catalog') {
      const rows = await listEnterpriseAssetCatalog(enterpriseId, token)
      if (scope !== scopeGeneration || request !== auxiliaryGeneration || token !== getToken()) return
      catalog.value = rows
    } else {
      const ownPromise = listMyEnterpriseAssetPublications(enterpriseId, token)
      const pendingPromise = canManage.value ? listPendingEnterpriseAssetPublications(enterpriseId, token) : Promise.resolve([])
      const [own, pending] = await Promise.all([ownPromise, pendingPromise])
      if (scope !== scopeGeneration || request !== auxiliaryGeneration || token !== getToken()) return
      myPublications.value = own; pendingPublications.value = pending
    }
  } catch (cause) {
    if (scope !== scopeGeneration || request !== auxiliaryGeneration || token !== getToken()) return
    if (isDenied(cause)) denied()
    else error.value = target === 'catalog' ? '企业目录加载失败，请重试。' : '发布申请加载失败，请重试。'
  } finally {
    if (scope === scopeGeneration && request === auxiliaryGeneration) auxiliaryLoading.value = false
  }
}
async function refresh() {
  if (view.value === 'drafts') await load()
  else await loadAuxiliary(view.value)
}
async function requestPublication(scope: 'enterprise' | 'global') {
  const row = selected.value
  if (!row || busy.value) return
  if (dirty.value) { error.value = '请先保存当前修改，再申请发布。'; return }
  const enterpriseId = props.enterpriseId, accountId = props.currentUserId, token = getToken()
  const generation = scopeGeneration
  busy.value = true; error.value = ''; message.value = ''
  let pending: { key: string; request: string } | undefined
  try {
    pending = await pendingKey(accountId, enterpriseId, `publish:${row.id}:${row.current_version}:${scope}`,
      { draft_id: row.id, expected_version: row.current_version, scope })
    if (generation !== scopeGeneration || token !== getToken()) return
    const result = await requestEnterpriseAssetPublication(enterpriseId, row.id,
      { scope, expected_version: row.current_version, request_key: pending.request }, token)
    clearPending(pending.key, pending.request)
    if (generation !== scopeGeneration || token !== getToken()) return
    view.value = 'requests'
    await loadAuxiliary('requests')
    await open(row.id)
    message.value = result.status === 'approved' ? '该版本已在此范围发布。' : '发布申请已提交，等待审批。'
  } catch (cause) {
    if (generation !== scopeGeneration || token !== getToken()) return
    if (isDenied(cause)) denied()
    else if ([409, 40901].includes(codeOf(cause))) error.value = '申请未创建：请核对当前草稿版本、已有申请和企业审批人设置后重试。'
    else if (!pending) error.value = '浏览器无法安全保存申请重试标识，请检查浏览器存储设置。'
    else error.value = '申请结果未知，可重试相同内容或刷新申请列表核对。'
  } finally { if (generation === scopeGeneration) busy.value = false }
}
async function withdraw(item: EnterpriseAssetPublication) {
  if (busy.value) return
  const generation = scopeGeneration, enterpriseId = props.enterpriseId, token = getToken()
  busy.value = true; error.value = ''; message.value = ''
  try {
    await withdrawEnterpriseAssetPublication(enterpriseId, item.id, token)
    if (generation !== scopeGeneration || token !== getToken()) return
    message.value = '发布申请已撤回。'; await loadAuxiliary('requests')
  } catch (cause) {
    if (generation !== scopeGeneration || token !== getToken()) return
    if (isDenied(cause)) denied()
    else error.value = '撤回结果未知，请刷新申请列表核对。'
  } finally { if (generation === scopeGeneration) busy.value = false }
}
async function decide(item: EnterpriseAssetPublication, decision: 'approve' | 'reject') {
  if (busy.value) return
  const generation = scopeGeneration, enterpriseId = props.enterpriseId, token = getToken()
  busy.value = true; error.value = ''; message.value = ''
  try {
    await decideEnterpriseAssetPublication(enterpriseId, item.id,
      { decision, reason: decisionReason.value.trim() }, token)
    if (generation !== scopeGeneration || token !== getToken()) return
    decisionReason.value = ''; message.value = decision === 'approve' ? '企业内发布已批准。' : '发布申请已拒绝。'
    await loadAuxiliary('requests')
  } catch (cause) {
    if (generation !== scopeGeneration || token !== getToken()) return
    if (isDenied(cause)) denied()
    else error.value = '审批结果未知，请刷新申请列表核对。'
  } finally { if (generation === scopeGeneration) busy.value = false }
}
async function revoke(item: EnterpriseAssetCatalogItem, scope: 'enterprise' | 'global') {
  if (busy.value) return
  const reason = window.prompt('撤销说明（可留空）', '')
  if (reason === null) return
  const generation = scopeGeneration, enterpriseId = props.enterpriseId, token = getToken()
  busy.value = true; error.value = ''; message.value = ''
  try {
    if (scope === 'enterprise') await revokeEnterpriseAssetPublication(enterpriseId, item.id, { reason: reason.trim() }, token)
    else throw new Error('unsupported scope')
    if (generation !== scopeGeneration || token !== getToken()) return
    message.value = '企业目录发布已撤销。'; await loadAuxiliary('catalog')
    if (selected.value) await open(selected.value.id)
  } catch (cause) {
    if (generation !== scopeGeneration || token !== getToken()) return
    if (isDenied(cause)) denied()
    else error.value = '撤销结果未知，请刷新目录核对。'
  } finally { if (generation === scopeGeneration) busy.value = false }
}
function newDraft() {
  if (!mayLeave()) return
  ++selectionGeneration; selected.value = null; editing.value = true; kind.value = 'agent'
  title.value = ''; definitionText.value = '{}'; error.value = ''; message.value = ''
  savedEditor = editorSnapshot()
}
async function open(id: number) {
  if (!mayLeave()) return
  const scope = scopeGeneration, request = ++selectionGeneration, enterpriseId = props.enterpriseId, token = getToken()
  const requestedEditor = editorSnapshot()
  error.value = ''; message.value = ''
  try {
    const row = await getEnterpriseAssetDraft(enterpriseId, id, token)
    if (scope !== scopeGeneration || request !== selectionGeneration || token !== getToken()) return
    if (editorSnapshot() !== requestedEditor) { error.value = '加载期间内容已修改，请保存后再切换。'; return }
    selected.value = row; kind.value = row.kind; title.value = row.title
    definitionText.value = JSON.stringify(row.definition, null, 2); editing.value = true
    savedEditor = editorSnapshot()
  } catch (cause) {
    if (scope !== scopeGeneration || request !== selectionGeneration || token !== getToken()) return
    if (editorSnapshot() !== requestedEditor) { error.value = '加载期间内容已修改，请保存后再切换。'; return }
    if (isDenied(cause)) denied()
    else { selected.value = null; editing.value = false; error.value = '草稿可能已删除，请刷新列表。' }
  }
}
function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical)
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value)
    .sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => [key, canonical(item)]))
  return value
}
async function pendingKey(accountId: number, enterpriseId: number, action: string, content: unknown) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(canonical(content))))
  const hash = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')
  const key = `prism:enterprise-asset:${accountId}:${enterpriseId}:${action}:${hash}`
  let request = sessionStorage.getItem(key)
  if (!request) { request = crypto.randomUUID(); sessionStorage.setItem(key, request) }
  if (sessionStorage.getItem(key) !== request) throw new Error('storage unavailable')
  return { key, request }
}
function clearPending(key: string, request: string) {
  try { if (sessionStorage.getItem(key) === request) sessionStorage.removeItem(key) } catch { /* 已保存。 */ }
}
async function save() {
  if (busy.value) return
  let definition: Record<string, unknown>
  try {
    const parsed: unknown = JSON.parse(definitionText.value)
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('not object')
    definition = parsed as Record<string, unknown>
    if (new TextEncoder().encode(JSON.stringify(definition)).length > 60000) throw new Error('too large')
  } catch { error.value = '请输入不超过 60000 字节的有效 JSON 对象。'; return }
  const name = title.value.trim()
  if (!name) { error.value = '请输入草稿名称。'; return }
  const scope = scopeGeneration, selectedId = selected.value?.id, expected = selected.value?.current_version
  const enterpriseId = props.enterpriseId, accountId = props.currentUserId, assetKind = kind.value, token = getToken()
  busy.value = true; error.value = ''; message.value = ''
  let pending: { key: string; request: string } | undefined
  try {
    pending = await pendingKey(accountId, enterpriseId, selectedId ? `revise:${selectedId}` : 'create',
      { kind: assetKind, title: name, definition, expected_version: expected })
    if (scope !== scopeGeneration || token !== getToken()) return
    if (selectedId && expected) {
      const version = await reviseEnterpriseAssetDraft(enterpriseId, selectedId,
        { title: name, definition, expected_version: expected, request_key: pending.request }, token)
      clearPending(pending.key, pending.request)
      if (scope !== scopeGeneration || token !== getToken()) return
      if (selected.value?.id === selectedId) selected.value = { ...selected.value, title: version.title,
        definition: version.definition, current_version: version.version_number }
    } else {
      const row = await createEnterpriseAssetDraft(enterpriseId,
        { kind: assetKind, title: name, definition, request_key: pending.request }, token)
      clearPending(pending.key, pending.request)
      if (scope !== scopeGeneration || token !== getToken()) return
      selected.value = row
    }
    savedEditor = editorSnapshot()
    message.value = '草稿已保存。'; await load()
  } catch (cause) {
    if (scope !== scopeGeneration || token !== getToken()) return
    if (isDenied(cause)) denied()
    else if ([409, 40901].includes(codeOf(cause))) error.value = '草稿版本已变化，请重新加载并核对；当前编辑内容仍保留。'
    else if (!pending) error.value = '浏览器无法安全保存重试标识，请检查浏览器存储设置。'
    else error.value = '保存结果未知，可直接重试相同内容，或刷新后核对。'
  } finally { if (scope === scopeGeneration) busy.value = false }
}
async function remove() {
  const row = selected.value
  if (!row || busy.value || !window.confirm(`确定删除草稿“${row.title}”吗？${dirty.value ? '未保存的修改也会丢失。' : ''}`)) return
  const scope = scopeGeneration, enterpriseId = props.enterpriseId, token = getToken()
  busy.value = true; error.value = ''; message.value = ''
  try {
    await deleteEnterpriseAssetDraft(enterpriseId, row.id, token)
    if (scope !== scopeGeneration || token !== getToken()) return
    selected.value = null; editing.value = false; title.value = ''; definitionText.value = '{}'
    message.value = '草稿已删除。'; await load()
  } catch (cause) {
    if (scope !== scopeGeneration || token !== getToken()) return
    if (isDenied(cause)) denied()
    else error.value = '删除结果未知，请刷新列表核对。'
  } finally { if (scope === scopeGeneration) busy.value = false }
}
watch([() => props.enterpriseId, () => props.currentUserId, () => props.role], () => {
  ++scopeGeneration; ++listGeneration; ++selectionGeneration; ++auxiliaryGeneration; clear(); scopeToken = getToken(); void load()
}, { immediate: true, flush: 'sync' })
function beforeUnload(event: BeforeUnloadEvent) {
  if (scopeToken !== getToken() || !dirty.value) return
  event.preventDefault(); event.returnValue = ''
}
function clearExpiredSession() {
  ++scopeGeneration; ++listGeneration; ++selectionGeneration; ++auxiliaryGeneration; clear(); scopeToken = null
}
onBeforeRouteLeave(() => mayLeave())
onMounted(() => {
  window.addEventListener('beforeunload', beforeUnload)
  window.addEventListener('prism:auth-expired', clearExpiredSession)
})
onBeforeUnmount(() => {
  ++scopeGeneration; ++listGeneration; ++selectionGeneration; ++auxiliaryGeneration
  window.removeEventListener('beforeunload', beforeUnload)
  window.removeEventListener('prism:auth-expired', clearExpiredSession)
})
</script>

<style scoped>
.assets { padding:20px; background:#fff; border:1px solid #e9eaf3; border-radius:14px; }
.heading,.actions { display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; }
h2 { margin:0; font-size:16px; } h3 { font-size:15px; margin:0 0 12px; }
.notice,.empty,aside p { color:#69718a; font-size:13px; line-height:1.6; }.error { color:#a23434; }
.columns { display:grid; grid-template-columns:260px minmax(0,1fr); gap:20px; margin-top:18px; }
button,input,select,textarea { font:inherit; border:1px solid #d9ddeb; border-radius:8px; padding:8px 10px; background:#fff; color:inherit; }
button { cursor:pointer; } button:disabled { cursor:default; opacity:.55; }.draft { width:100%; text-align:left; margin-bottom:8px; }
.draft[aria-pressed='true'] { background:#eff0ff; }.editor form { display:grid; gap:9px; }.editor textarea { resize:vertical; font-family:monospace; }
@media (max-width: 700px) { .columns { grid-template-columns:1fr; } }
</style>
