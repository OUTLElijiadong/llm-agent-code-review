<template>
  <section class="knowledge">
    <div class="heading"><h2>企业知识库</h2><button :disabled="busy" @click="refresh">刷新</button></div>
    <p class="muted">企业内共享知识仅对本企业成员开放。附件支持 TXT、Markdown、DOCX 和可提取文字的 PDF；扫描件 OCR、外部语义嵌入暂不支持，小菱可检索命中的知识片段。</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <button v-if="conflictKey" type="button" @click="discardConflict">放弃旧创建请求</button>
    <p v-if="uncertain" class="error">上次修改结果未知，请刷新并核对目标条目后再操作。</p>
    <button v-if="uncertain && checked" type="button" @click="acknowledge">已核对结果，继续操作</button>
    <section v-if="!blocked" class="attachments" aria-labelledby="attachments-heading">
      <div class="heading"><h3 id="attachments-heading">企业知识附件</h3>
        <button type="button" :disabled="attachmentBusy" @click="loadAttachments">刷新附件</button></div>
      <form class="search" data-action="attachment-search" @submit.prevent="searchAttachments">
        <label for="attachment-query">搜索附件</label>
        <input id="attachment-query" v-model.trim="attachmentQueryInput" :disabled="attachmentBusy" maxlength="100" placeholder="搜索附件内容" />
        <button :disabled="attachmentBusy">搜索附件</button>
      </form>
      <p class="muted">本地词项与字符相似度检索，不上传文件到 embedding 服务。单文件最多 8 MB、最多提取 50 万字符；PDF 最多 120 页；企业检索库最多 10,000 个有效片段。扫描件或加密 PDF 不支持。</p>
      <p v-if="attachmentError" class="error" role="alert">{{ attachmentError }}</p>
      <button v-if="attachmentConflictKey" type="button" :disabled="attachmentBusy" @click="discardAttachmentConflict">放弃旧上传请求并允许重新上传</button>
      <p v-if="attachmentUncertain" class="muted" role="status">上次上传结果未知；重新选择同一文件会使用相同请求键安全重试。</p>
      <form v-if="canManage" class="upload" data-action="attachment-upload" @submit.prevent="uploadAttachment">
        <label for="knowledge-attachment">上传企业共享附件</label>
        <input id="knowledge-attachment" type="file" accept=".txt,.md,.docx,.pdf" :disabled="attachmentBusy" @change="selectAttachment" />
        <span v-if="selectedFile" class="muted">{{ selectedFile.name }} · {{ formatBytes(selectedFile.size) }}</span>
        <button :disabled="attachmentBusy || !selectedFile || Boolean(attachmentConflictKey)">{{ attachmentBusy ? '处理中…' : '上传附件' }}</button>
      </form>
      <p v-if="attachmentLoading" role="status">正在加载附件…</p>
      <ul v-else class="attachment-list">
        <template v-if="attachmentQuery">
          <li v-for="item in attachmentResults" :key="`hit-${item.attachment_id}`" class="attachment-hit">
            <strong>{{ item.file_name }}</strong><p class="body">{{ item.excerpt }}</p>
            <span class="muted">{{ item.search_mode === 'local_hash_vector_and_terms' ? '本地相似度命中' : '本地检索命中' }}</span>
            <button type="button" :disabled="attachmentBusy"
              @click="downloadAttachment({ id: item.attachment_id, file_name: item.file_name })">下载附件</button>
          </li>
          <li v-if="!attachmentResults.length" class="muted">没有找到匹配附件。</li>
        </template>
        <template v-else>
          <li v-for="item in attachments" :key="`file-${item.id}`" class="attachment-row">
          <div><strong>{{ item.file_name }}</strong><span class="muted">{{ formatBytes(item.file_size) }} · v{{ item.version }}</span></div>
          <div class="actions"><button type="button" :disabled="attachmentBusy" @click="downloadAttachment(item)">下载</button>
            <button v-if="canManage" type="button" :disabled="attachmentBusy" @click="removeAttachment(item)">删除</button></div>
          </li>
          <li v-if="!attachments.length" class="muted">暂无企业附件。</li>
          <li v-if="attachmentHasMore"><button type="button" :disabled="attachmentPageLoading || attachmentBusy"
            @click="moreAttachments">{{ attachmentPageLoading ? '正在加载…' : '加载更多附件' }}</button></li>
        </template>
      </ul>
    </section>
    <form class="search" data-action="search" @submit.prevent="search"><label for="knowledge-query">搜索内容</label>
      <input id="knowledge-query" v-model.trim="queryInput" :disabled="busy" maxlength="100" placeholder="输入标题或正文中的词" />
      <button :disabled="busy">搜索</button></form>
    <p v-if="loading" role="status">正在加载知识…</p>
    <template v-else-if="!blocked">
      <div class="columns">
        <section><h3>{{ query ? '搜索结果' : '知识条目' }}</h3>
          <p v-if="!items.length" class="muted">暂无条目。</p>
          <button v-for="item in items" :key="item.id" type="button" class="entry" :disabled="busy" @click="open(item.id)">{{ item.title }}</button>
          <button v-if="hasMore" :disabled="pageLoading || busy" @click="more">加载更多</button>
        </section>
        <section><template v-if="selected"><h3>{{ selected.title }}</h3><p class="body">{{ selected.content }}</p>
          <div v-if="canManage" class="actions"><button :disabled="busy || uncertain" @click="edit">编辑</button>
            <button :disabled="busy || uncertain" @click="remove">删除</button></div></template>
          <p v-else class="muted">选择条目查看正文。</p></section>
      </div>
      <form v-if="canManage" data-action="save" class="editor" @submit.prevent="save">
        <h3>{{ editing ? '编辑知识' : '新建知识' }}</h3>
        <label for="knowledge-title">标题</label><input id="knowledge-title" v-model.trim="title" :disabled="busy" maxlength="200" />
        <label for="knowledge-content">正文</label><textarea id="knowledge-content" v-model="content" :disabled="busy" maxlength="50000" rows="5" />
        <div class="actions"><button :disabled="busy || uncertain || !title || !content.trim()">{{ busy ? '保存中…' : '保存' }}</button>
          <button v-if="editing" type="button" :disabled="busy" @click="resetEditor">取消编辑</button></div>
      </form>
    </template>
  </section>
</template>

<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { getToken } from '@/utils/token'
import { createEnterpriseKnowledge, deleteEnterpriseKnowledge, getEnterpriseKnowledge,
  deleteEnterpriseKnowledgeAttachment, downloadEnterpriseKnowledgeAttachment,
  listEnterpriseKnowledge, listEnterpriseKnowledgeAttachments, searchEnterpriseKnowledgeAttachments,
  updateEnterpriseKnowledge, uploadEnterpriseKnowledgeAttachment,
  type EnterpriseKnowledgeAttachment, type EnterpriseKnowledgeAttachmentHit, type EnterpriseKnowledgeEntry } from '@/api/enterpriseKnowledge'

const props = defineProps<{ enterpriseId: number; currentUserId: number; canManage: boolean }>()
const emit = defineEmits<{ 'access-lost': [] }>()
const items = ref<EnterpriseKnowledgeEntry[]>([]), selected = ref<EnterpriseKnowledgeEntry | null>(null)
const loading = ref(false), busy = ref(false), blocked = ref(false), error = ref('')
const hasMore = ref(false), pageLoading = ref(false), uncertain = ref(false), checked = ref(false)
const queryInput = ref(''), query = ref(''), title = ref(''), content = ref(''), editing = ref<number | null>(null)
const conflictKey = ref('')
const editingVersion = ref<number | null>(null)
const attachments = ref<EnterpriseKnowledgeAttachment[]>([]), attachmentResults = ref<EnterpriseKnowledgeAttachmentHit[]>([])
const attachmentQueryInput = ref(''), attachmentQuery = ref(''), attachmentError = ref('')
const attachmentLoading = ref(false), attachmentBusy = ref(false), attachmentUncertain = ref(false)
const attachmentHasMore = ref(false), attachmentPageLoading = ref(false)
const attachmentConflictKey = ref(''), selectedFile = ref<File | null>(null)
let generation = 0, detailGeneration = 0, attachmentGeneration = 0
let uncertaintyMarker: string | null = null
const pending = new Map<string, string>()
const pendingAttachmentKeys = new Map<string, string>()

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / (1024 * 1024)).toFixed(1)} MB`
}

function resetEditor() { editing.value = null; editingVersion.value = null; title.value = ''; content.value = '' }
function uncertaintyKey(account: number, company: number) { return `prism:uncertain-knowledge:${account}:${company}` }
function recordUncertain(account: number, company: number) {
  const marker = crypto.randomUUID()
  try { sessionStorage.setItem(uncertaintyKey(account, company), marker) } catch { /* 当前页面仍会保持锁定 */ }
  if (props.currentUserId === account && props.enterpriseId === company) {
    uncertaintyMarker = marker; uncertain.value = true; checked.value = false
  }
  return marker
}
function clearUncertain(account: number, company: number, expected?: string) {
  let stored: string | null = null
  try { stored = sessionStorage.getItem(uncertaintyKey(account, company)) } catch { /* 无可用会话存储 */ }
  if (expected && (stored || uncertaintyMarker) !== expected) return
  try { sessionStorage.removeItem(uncertaintyKey(account, company)) } catch { /* 无可用会话存储 */ }
  if (props.currentUserId === account && props.enterpriseId === company) {
    uncertaintyMarker = null; uncertain.value = false; checked.value = false
  }
}
function acknowledge() { clearUncertain(props.currentUserId, props.enterpriseId) }
function errorCode(cause: unknown) { return typeof cause === 'object' && cause !== null && 'code' in cause ? Number(cause.code) : 0 }
function knownRejection(cause: unknown) {
  const code = errorCode(cause)
  return (code >= 40000 && code < 50000) || (code >= 400 && code < 500)
}
function fail(cause: unknown, message: string) {
  const code = errorCode(cause)
  if ((code >= 40300 && code < 40400) || code === 403) {
    ++generation; ++detailGeneration; items.value = []; selected.value = null; resetEditor()
    ++attachmentGeneration; attachments.value = []; attachmentResults.value = []; attachmentBusy.value = false
    attachmentLoading.value = false
    blocked.value = true; loading.value = false; error.value = '知识访问权限已变化，请刷新企业列表。'
    attachmentError.value = '知识访问权限已变化，请刷新企业列表。'
    emit('access-lost')
  } else if ((code >= 40400 && code < 40500) || code === 404) {
    ++detailGeneration; selected.value = null; resetEditor(); error.value = '知识条目不存在或已删除，请刷新列表。'
  } else error.value = message
}
async function load() {
  const current = ++generation, company = props.enterpriseId, keyword = query.value
  ++detailGeneration; selected.value = null; loading.value = true; error.value = ''; pageLoading.value = false
  try {
    const result = await listEnterpriseKnowledge(company, keyword)
    if (current === generation) { items.value = result.items; hasMore.value = result.items.length === 30 }
  } catch (cause) { if (current === generation) fail(cause, '知识加载失败，请重试。') }
  finally { if (current === generation) loading.value = false }
}
async function refresh() {
  await Promise.all([load(), loadAttachments()])
  if (!error.value && !blocked.value && uncertain.value) checked.value = true
}
async function search() { query.value = queryInput.value.trim(); await load() }

function attachmentStorageKey(account: number, company: number, hash: string) {
  return `prism:pending-knowledge-attachment:${account}:${company}:${hash}`
}
async function attachmentRequestKey(account: number, company: number, file: File) {
  const bytes = await file.arrayBuffer()
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  const hash = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')
  const storageKey = attachmentStorageKey(account, company, `${file.name}:${hash}`)
  let requestId = pendingAttachmentKeys.get(storageKey)
  try { requestId ||= sessionStorage.getItem(storageKey) || undefined } catch { /* 降级为当前页面内重试 */ }
  if (!requestId) {
    requestId = crypto.randomUUID()
    try { sessionStorage.setItem(storageKey, requestId) } catch { /* 仅保留内存 */ }
  }
  pendingAttachmentKeys.set(storageKey, requestId)
  return { storageKey, requestId }
}
function clearAttachmentRequestKey(storageKey: string) {
  pendingAttachmentKeys.delete(storageKey)
  try { sessionStorage.removeItem(storageKey) } catch { /* 已保存 */ }
}
function selectAttachment(event: Event) {
  const input = event.target as HTMLInputElement
  selectedFile.value = input.files?.[0] || null
  attachmentError.value = ''
  attachmentConflictKey.value = ''
  attachmentUncertain.value = false
}
function discardAttachmentConflict() {
  if (!attachmentConflictKey.value || !window.confirm('确认放弃旧请求，并为该文件生成新的上传请求？')) return
  clearAttachmentRequestKey(attachmentConflictKey.value)
  attachmentConflictKey.value = ''
  attachmentUncertain.value = false
  attachmentError.value = ''
}
async function loadAttachments() {
  const current = ++attachmentGeneration, company = props.enterpriseId
  const token = getToken(), keyword = attachmentQuery.value
  attachmentLoading.value = true
  attachmentError.value = ''
  try {
    if (keyword) {
      const result = await searchEnterpriseKnowledgeAttachments(company, keyword, token)
      if (current === attachmentGeneration && token === getToken()) {
        attachmentResults.value = result.items
        attachments.value = []
      }
    } else {
      const result = await listEnterpriseKnowledgeAttachments(company, undefined, token)
      if (current === attachmentGeneration && token === getToken()) {
        attachments.value = result.items
        attachmentResults.value = []
        attachmentHasMore.value = result.items.length === 30
      }
    }
  } catch (cause) {
    if (current === attachmentGeneration && token === getToken()) {
      const code = errorCode(cause)
      if ((code >= 40300 && code < 40400) || code === 403) fail(cause, '附件加载失败。')
      else attachmentError.value = '附件加载失败，请重试。'
    }
  } finally {
    if (current === attachmentGeneration) attachmentLoading.value = false
  }
}
async function moreAttachments() {
  const current = attachmentGeneration, company = props.enterpriseId, token = getToken()
  const beforeId = attachments.value.at(-1)?.id
  if (!beforeId || !attachmentHasMore.value || attachmentPageLoading.value || attachmentQuery.value) return
  attachmentPageLoading.value = true
  try {
    const result = await listEnterpriseKnowledgeAttachments(company, beforeId, token)
    if (current !== attachmentGeneration || token !== getToken()) return
    const known = new Set(attachments.value.map(item => item.id))
    attachments.value.push(...result.items.filter(item => !known.has(item.id)))
    attachmentHasMore.value = result.items.length === 30
  } catch {
    if (current === attachmentGeneration && token === getToken()) attachmentError.value = '加载更多附件失败，请重试。'
  } finally { if (current === attachmentGeneration) attachmentPageLoading.value = false }
}
async function searchAttachments() {
  attachmentQuery.value = attachmentQueryInput.value.trim()
  await loadAttachments()
}
async function uploadAttachment() {
  const file = selectedFile.value
  if (!file || !props.canManage || attachmentBusy.value) return
  if (file.size < 1 || file.size > 8 * 1024 * 1024) {
    attachmentError.value = '附件必须大于 0 字节且不超过 8 MB。'
    return
  }
  const current = attachmentGeneration, company = props.enterpriseId, account = props.currentUserId, token = getToken()
  attachmentBusy.value = true; attachmentError.value = ''
  let request: Awaited<ReturnType<typeof attachmentRequestKey>> | undefined
  try {
    request = await attachmentRequestKey(account, company, file)
    if (current !== attachmentGeneration || token !== getToken()) return
    await uploadEnterpriseKnowledgeAttachment(company, file, request.requestId, token)
    clearAttachmentRequestKey(request.storageKey)
    if (current === attachmentGeneration && token === getToken()) {
      selectedFile.value = null; attachmentUncertain.value = false
      const input = document.querySelector<HTMLInputElement>('#knowledge-attachment')
      if (input) input.value = ''
      attachmentBusy.value = false
      await loadAttachments()
    }
  } catch (cause) {
    if (current === attachmentGeneration && token === getToken()) {
      const code = errorCode(cause)
      if ((code >= 40900 && code < 41000) || code === 409) {
        attachmentConflictKey.value = request?.storageKey || ''
        attachmentError.value = '该上传请求键已用于其他内容或已删除附件；确认核对后可放弃旧请求再上传。'
      } else if (knownRejection(cause)) {
        if (request) clearAttachmentRequestKey(request.storageKey)
        attachmentError.value = '上传被拒绝，请检查文件格式、大小或当前权限。'
      } else {
        attachmentUncertain.value = true
        attachmentError.value = '上传结果暂时未知；保留同一请求键，重试不会重复创建附件。'
      }
      if ((code >= 40300 && code < 40400) || code === 403) fail(cause, '附件上传失败。')
    }
  } finally {
    if (current === attachmentGeneration) attachmentBusy.value = false
  }
}
async function downloadAttachment(item: Pick<EnterpriseKnowledgeAttachment, 'id' | 'file_name'>) {
  const current = attachmentGeneration, company = props.enterpriseId, token = getToken()
  attachmentBusy.value = true; attachmentError.value = ''
  try {
    const blob = await downloadEnterpriseKnowledgeAttachment(company, item.id, token)
    if (current !== attachmentGeneration || token !== getToken()) return
    const url = URL.createObjectURL(blob), link = document.createElement('a')
    link.href = url; link.download = item.file_name; link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 0)
  } catch (cause) {
    if (current === attachmentGeneration && token === getToken()) {
      const code = errorCode(cause)
      if ((code >= 40300 && code < 40400) || code === 403) fail(cause, '附件下载失败。')
      else attachmentError.value = '附件下载失败或权限已变化，请刷新后重试。'
    }
  } finally { if (current === attachmentGeneration) attachmentBusy.value = false }
}
async function removeAttachment(item: EnterpriseKnowledgeAttachment) {
  if (!props.canManage || attachmentBusy.value || !window.confirm(
    `确定永久删除“${item.file_name}”的原文件、提取文本和检索片段？已经产生的公司聊天消息和小菱历史答复仍会保留。`,
  )) return
  const current = attachmentGeneration, company = props.enterpriseId, token = getToken()
  attachmentBusy.value = true; attachmentError.value = ''
  try {
    await deleteEnterpriseKnowledgeAttachment(company, item.id, item.version, token)
    if (current === attachmentGeneration && token === getToken()) {
      attachmentBusy.value = false
      await loadAttachments()
    }
  } catch (cause) {
    if (current === attachmentGeneration && token === getToken()) {
      const code = errorCode(cause)
      if ((code >= 40300 && code < 40400) || code === 403) fail(cause, '附件删除失败。')
      else attachmentError.value = '附件删除未确认，请刷新核对附件是否已移除。'
    }
  } finally { if (current === attachmentGeneration) attachmentBusy.value = false }
}
async function more() {
  const current = generation, last = items.value.at(-1)?.id
  if (!last || !hasMore.value || pageLoading.value) return
  pageLoading.value = true
  try {
    const result = await listEnterpriseKnowledge(props.enterpriseId, query.value, last)
    if (current !== generation) return
    const seen = new Set(items.value.map(item => item.id))
    items.value.push(...result.items.filter(item => !seen.has(item.id))); hasMore.value = result.items.length === 30
  } catch (cause) { if (current === generation) fail(cause, '加载更多知识失败，请重试。') }
  finally { if (current === generation) pageLoading.value = false }
}
async function open(id: number) {
  const current = generation, detail = ++detailGeneration
  try {
    const result = await getEnterpriseKnowledge(props.enterpriseId, id)
    if (current === generation && detail === detailGeneration) { selected.value = result; error.value = '' }
  } catch (cause) { if (current === generation && detail === detailGeneration) fail(cause, '知识详情加载失败，请重试。') }
}
function edit() {
  if (!selected.value) return
  editing.value = selected.value.id; editingVersion.value = selected.value.version
  title.value = selected.value.title; content.value = selected.value.content || ''
}
async function requestKey(account: number, company: number, titleValue: string, body: string) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify([titleValue, body])))
  const hash = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')
  const key = `prism:pending-knowledge:${account}:${company}:${hash}`
  let id = pending.get(key)
  try { id ||= sessionStorage.getItem(key) || undefined } catch { /* 降级为当前页面内重试 */ }
  if (!id) { id = crypto.randomUUID(); try { sessionStorage.setItem(key, id) } catch { /* 仅保留内存 */ } }
  pending.set(key, id)
  return { key, id }
}
function clearKey(key: string) { pending.delete(key); try { sessionStorage.removeItem(key) } catch { /* 已保存 */ } }
function discardConflict() {
  if (!conflictKey.value || !window.confirm('确认放弃旧请求，并允许重新创建相同内容？')) return
  clearKey(conflictKey.value); conflictKey.value = ''; error.value = ''
}
async function save() {
  const current = generation, company = props.enterpriseId, account = props.currentUserId, token = getToken()
  const titleValue = title.value.trim(), body = content.value.trim(), editId = editing.value, version = editingVersion.value
  if (!props.canManage || !titleValue || !body || busy.value || uncertain.value) return
  busy.value = true; error.value = ''; conflictKey.value = ''
  let createKey = ''
  let mutationMarker: string | undefined
  try {
    if (editId && version) {
      mutationMarker = recordUncertain(account, company)
      await updateEnterpriseKnowledge(company, editId, titleValue, body, version, token)
      clearUncertain(account, company, mutationMarker)
    } else if (editId) {
      error.value = '条目版本缺失，请刷新后重新编辑。'
      return
    }
    else {
      const request = await requestKey(account, company, titleValue, body)
      createKey = request.key
      if (current !== generation || token !== getToken()) return
      await createEnterpriseKnowledge(company, titleValue, body, request.id, token)
      clearKey(request.key)
    }
    if (current === generation && token === getToken()) { resetEditor(); busy.value = false; await load() }
  } catch (cause) {
    if (mutationMarker && knownRejection(cause)) clearUncertain(account, company, mutationMarker)
    if (current === generation && token === getToken()) {
      const code = errorCode(cause)
      if (!editId && createKey && ((code >= 40900 && code < 41000) || code === 409)) conflictKey.value = createKey
      fail(cause, editId ? '修改失败，请刷新并核对。' : '创建失败，可重试同一内容。')
    }
  } finally { if (current === generation) busy.value = false }
}
async function remove() {
  if (!selected.value || !props.canManage || busy.value || uncertain.value || !window.confirm('确定删除此知识条目？')) return
  const current = generation, company = props.enterpriseId, account = props.currentUserId, id = selected.value.id, token = getToken()
  busy.value = true
  let mutationMarker: string | undefined
  const version = selected.value.version
  try {
    if (!version) { error.value = '条目版本缺失，请刷新后重试。'; return }
    mutationMarker = recordUncertain(account, company)
    await deleteEnterpriseKnowledge(company, id, version, token)
    clearUncertain(account, company, mutationMarker)
    if (current === generation && token === getToken()) { resetEditor(); busy.value = false; await load() }
  } catch (cause) {
    if (mutationMarker && knownRejection(cause)) clearUncertain(account, company, mutationMarker)
    if (current === generation && token === getToken()) fail(cause, '删除失败，请刷新并核对。')
  }
  finally { if (current === generation) busy.value = false }
}
watch(() => [props.enterpriseId, props.currentUserId] as const, () => {
  ++generation; ++detailGeneration; items.value = []; selected.value = null; resetEditor(); pending.clear()
  blocked.value = false; busy.value = false; checked.value = false; conflictKey.value = ''; query.value = ''; queryInput.value = ''
  ++attachmentGeneration; attachments.value = []; attachmentResults.value = []; attachmentQuery.value = ''; attachmentQueryInput.value = ''
  attachmentError.value = ''; attachmentBusy.value = false; attachmentUncertain.value = false
  attachmentHasMore.value = false; attachmentPageLoading.value = false
  attachmentConflictKey.value = ''; selectedFile.value = null; pendingAttachmentKeys.clear()
  try { uncertaintyMarker = sessionStorage.getItem(uncertaintyKey(props.currentUserId, props.enterpriseId)) }
  catch { uncertaintyMarker = null }
  uncertain.value = Boolean(uncertaintyMarker)
  void load(); void loadAttachments()
}, { immediate: true, flush: 'sync' })
onBeforeUnmount(() => { ++generation; ++detailGeneration; ++attachmentGeneration })
</script>

<style scoped>
.knowledge { padding:20px; background:white; border:1px solid #e9eaf3; border-radius:14px; }
.heading,.search,.actions { display:flex; align-items:center; flex-wrap:wrap; gap:10px; }.heading { justify-content:space-between; }
h2 { margin:0; font-size:16px; } h3 { font-size:14px; }.muted { color:#69718a; font-size:13px; line-height:1.6; }.error { color:#a23434; }
button,input,textarea { font:inherit; border:1px solid #d9ddeb; background:white; border-radius:8px; padding:8px 10px; color:inherit; }
.columns { display:grid; grid-template-columns:minmax(150px, 1fr) minmax(0, 2fr); gap:16px; margin:18px 0; }.entry { display:block; width:100%; text-align:left; margin-bottom:8px; }
.body { white-space:pre-wrap; overflow-wrap:anywhere; line-height:1.7; }.editor { display:grid; gap:8px; max-width:740px; border-top:1px solid #eee; padding-top:14px; }
.attachments { border-top:1px solid #e6e9f1; border-bottom:1px solid #e6e9f1; margin:18px 0; padding:14px 0; display:grid; gap:10px; }
.upload { display:flex; align-items:center; flex-wrap:wrap; gap:10px; }.attachment-list { list-style:none; margin:0; padding:0; display:grid; gap:8px; }
.attachment-row,.attachment-hit { border:1px solid #e7eaf2; border-radius:10px; padding:10px; display:flex; align-items:center; justify-content:space-between; gap:12px; }
.attachment-row > div:first-child { display:grid; gap:4px; min-width:0; }.attachment-row strong,.attachment-hit strong { overflow-wrap:anywhere; }
.attachment-hit { display:block; }.attachment-hit .body { margin:6px 0; }
@media(max-width:680px) { .columns { grid-template-columns:1fr; } .search input { min-width:0; max-width:100%; } }
</style>
