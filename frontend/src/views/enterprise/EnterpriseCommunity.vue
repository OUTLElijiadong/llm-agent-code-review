<template>
  <section class="community-card">
    <div class="head"><h2>{{ title() }}</h2><button type="button" :disabled="busy" @click="load">刷新</button></div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" class="muted" role="status">{{ notice }}</p>
    <p v-if="loading" class="muted" role="status">正在加载…</p>
    <template v-else-if="!blocked">
      <article v-if="selected && kind === 'forum'" class="detail">
        <button type="button" @click="closePost">返回帖子列表</button>
        <h3>{{ selected.post.title }}</h3><p class="meta">{{ author(selected.post.author_id) }}</p>
        <p class="body">{{ selected.post.content }}</p>
        <div v-if="canModify(selected.post.author_id)" class="actions">
          <button type="button" @click="edit(selected.post)">编辑</button>
          <button type="button" @click="remove(selected.post)">删除</button>
        </div>
        <h3>回复</h3><p v-if="!selected.replies.length" class="muted">暂无回复。</p>
        <article v-for="reply in selected.replies" :key="reply.id" class="reply">
          <span class="meta">{{ author(reply.author_id) }}</span><p class="body">{{ reply.content }}</p>
          <button v-if="canModify(reply.author_id)" type="button" @click="removeReply(reply)">删除回复</button>
        </article>
        <button v-if="hasMoreReplies" type="button" :disabled="replyLoading" @click="loadMoreReplies">
          {{ replyLoading ? '加载中…' : '加载更多回复' }}
        </button>
        <form @submit.prevent="submitReply"><label for="reply-content">回复内容</label>
          <textarea id="reply-content" v-model="replyText" maxlength="20000" rows="3" />
          <button type="submit" :disabled="busy || !replyText.trim()">发表回复</button>
        </form>
      </article>
      <template v-else>
        <p v-if="!items.length" class="muted">{{ kind === 'forum' ? '暂无帖子。' : '暂无公告。' }}</p>
        <article v-for="item in items" :key="item.id" class="item">
          <button v-if="kind === 'forum'" type="button" class="item-title" @click="open(item.id)">{{ item.title }}</button>
          <h3 v-else>{{ item.title }}</h3>
          <span class="meta">{{ author(item.author_id) }}</span>
          <p class="body">{{ item.content }}</p>
          <div v-if="kind === 'forum' ? canModify(item.author_id) : canManage" class="actions">
            <button type="button" @click="edit(item)">编辑</button><button type="button" @click="remove(item)">删除</button>
          </div>
        </article>
        <button v-if="hasMoreItems" type="button" :disabled="pageLoading" @click="loadMoreItems">
          {{ pageLoading ? '加载中…' : `加载更多${kind === 'forum' ? '帖子' : '公告'}` }}
        </button>
      </template>
      <form v-if="!selected && (kind === 'forum' || canManage)" class="editor" @submit.prevent="submit">
        <h3>{{ editId ? '编辑' : '发布' }}{{ kind === 'forum' ? '帖子' : '公告' }}</h3>
        <label for="content-title">标题</label><input id="content-title" v-model.trim="formTitle" maxlength="200" />
        <label for="content-body">内容</label><textarea id="content-body" v-model="formContent" maxlength="20000" rows="4" />
        <div class="actions"><button type="submit" :disabled="busy || !formTitle || !formContent.trim()">{{ busy ? '保存中…' : '保存' }}</button>
          <button v-if="editId" type="button" @click="resetForm">取消编辑</button></div>
      </form>
    </template>
  </section>
</template>
<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { getToken } from '@/utils/token'
import {
  createEnterpriseAnnouncement, createEnterprisePost, deleteEnterpriseAnnouncement, deleteEnterprisePost,
  deleteEnterpriseReply, getEnterprisePost, listEnterpriseAnnouncements, listEnterprisePosts, replyEnterprisePost,
  updateEnterpriseAnnouncement, updateEnterprisePost, type EnterpriseContent, type EnterpriseDirectoryMember,
  type EnterpriseReply,
} from '@/api/enterprise'

const props = defineProps<{ enterpriseId: number; kind: 'forum' | 'announcement'; canManage: boolean;
  currentUserId: number; directory: EnterpriseDirectoryMember[] }>()
const emit = defineEmits<{ (event: 'access-lost'): void }>()
const items = ref<EnterpriseContent[]>([])
const selected = ref<{ post: EnterpriseContent; replies: EnterpriseReply[] } | null>(null)
const loading = ref(false), busy = ref(false), blocked = ref(false), error = ref('')
const pageLoading = ref(false), replyLoading = ref(false), hasMoreItems = ref(false), hasMoreReplies = ref(false)
const notice = ref('')
const editId = ref<number | null>(null), formTitle = ref(''), formContent = ref(''), replyText = ref('')
let generation = 0
let detailGeneration = 0
const pendingKeys = new Map<string, string>()
async function pendingKey(userId: number, companyId: number, action: string, content: string[]) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(content)))
  const hash = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')
  const storageKey = `prism:pending-community:${userId}:${companyId}:${action}:${hash}`
  let id = pendingKeys.get(storageKey)
  try { id ||= sessionStorage.getItem(storageKey) || undefined } catch { /* 降级为本页内重试。 */ }
  if (!id) {
    id = crypto.randomUUID()
    try { sessionStorage.setItem(storageKey, id) } catch { /* 浏览器禁用存储时无法跨刷新重试。 */ }
  }
  pendingKeys.set(storageKey, id)
  return { storageKey, id }
}
function clearPendingKey(storageKey: string) {
  pendingKeys.delete(storageKey)
  try { sessionStorage.removeItem(storageKey) } catch { /* 不影响已确认的写入。 */ }
}
const title = () => props.kind === 'forum' ? '企业论坛' : '企业公告'
const author = (id: number) => props.directory.find(item => item.user_id === id)?.display_name || `成员 ${id}`
const canModify = (id: number) => props.canManage || id === props.currentUserId
function resetForm() { editId.value = null; formTitle.value = ''; formContent.value = '' }
function closePost() { ++detailGeneration; selected.value = null; replyText.value = ''; replyLoading.value = false; notice.value = '' }
function failed(cause: unknown, message: string) {
  const code = typeof cause === 'object' && cause !== null && 'code' in cause ? Number(cause.code) : 0
  if ((code >= 40300 && code < 40500) || code === 403 || code === 404) {
    ++generation; ++detailGeneration
    items.value = []; selected.value = null; replyText.value = ''; notice.value = ''; resetForm()
    hasMoreItems.value = false; hasMoreReplies.value = false; pageLoading.value = false; replyLoading.value = false
    blocked.value = true; loading.value = false; error.value = '企业内容权限已变化，请刷新企业列表。'
    emit('access-lost')
  } else error.value = message
}
async function load() {
  const current = ++generation, id = props.enterpriseId, kind = props.kind
  loading.value = true; pageLoading.value = false; error.value = ''; notice.value = ''
  try {
    const result = kind === 'forum' ? await listEnterprisePosts(id) : await listEnterpriseAnnouncements(id)
    if (current === generation) { items.value = result.items; hasMoreItems.value = result.items.length === 50 }
  } catch (cause) { if (current === generation) failed(cause, '内容加载失败，请重试。') }
  finally { if (current === generation) loading.value = false }
}
async function loadMoreItems() {
  const lastId = items.value.at(-1)?.id, current = generation, id = props.enterpriseId, kind = props.kind
  if (!lastId || pageLoading.value || !hasMoreItems.value) return
  pageLoading.value = true
  try {
    const result = kind === 'forum' ? await listEnterprisePosts(id, lastId) : await listEnterpriseAnnouncements(id, lastId)
    if (current === generation) {
      const existing = new Set(items.value.map(item => item.id))
      items.value.push(...result.items.filter(item => !existing.has(item.id)))
      hasMoreItems.value = result.items.length === 50
    }
  } catch (cause) { if (current === generation) failed(cause, '加载更多内容失败，请重试。') }
  finally { if (current === generation) pageLoading.value = false }
}
async function open(id: number) {
  const current = generation, detail = ++detailGeneration, company = props.enterpriseId
  replyLoading.value = false; notice.value = ''
  try {
    const result = await getEnterprisePost(company, id)
    if (current === generation && detail === detailGeneration) {
      selected.value = result; hasMoreReplies.value = result.replies.length === 100; error.value = ''
    }
  } catch (cause) { if (current === generation && detail === detailGeneration) failed(cause, '帖子加载失败，请重试。') }
}
async function loadMoreReplies() {
  const postId = selected.value?.post.id, after = selected.value?.replies.at(-1)?.id
  const current = generation, detail = detailGeneration, company = props.enterpriseId
  if (!postId || !after || replyLoading.value || !hasMoreReplies.value) return
  replyLoading.value = true
  try {
    const result = await getEnterprisePost(company, postId, after)
    if (current === generation && detail === detailGeneration && selected.value?.post.id === postId) {
      const existing = new Set(selected.value.replies.map(reply => reply.id))
      selected.value.replies.push(...result.replies.filter(reply => !existing.has(reply.id)))
      hasMoreReplies.value = result.replies.length === 100
    }
  } catch (cause) { if (current === generation && detail === detailGeneration) failed(cause, '加载更多回复失败，请重试。') }
  finally { if (current === generation && detail === detailGeneration) replyLoading.value = false }
}
function edit(item: EnterpriseContent) {
  ++detailGeneration; selected.value = null; editId.value = item.id; formTitle.value = item.title; formContent.value = item.content
}
async function submit() {
  const current = generation, id = props.enterpriseId, kind = props.kind
  const userId = props.currentUserId, editing = editId.value
  const token = getToken()
  const titleValue = formTitle.value.trim(), content = formContent.value.trim()
  if (!titleValue || !content || busy.value) return
  busy.value = true; error.value = ''
  try {
    if (editing) {
      if (kind === 'forum') await updateEnterprisePost(id, editing, titleValue, content, token)
      else await updateEnterpriseAnnouncement(id, editing, titleValue, content, token)
    } else {
      const request = await pendingKey(userId, id, kind, [titleValue, content])
      if (current !== generation || token !== getToken()) return
      if (kind === 'forum') await createEnterprisePost(id, titleValue, content, request.id, token)
      else await createEnterpriseAnnouncement(id, titleValue, content, request.id, token)
      clearPendingKey(request.storageKey)
    }
    if (current === generation && token === getToken()) {
      if (editId.value === editing && formTitle.value.trim() === titleValue && formContent.value.trim() === content) resetForm()
      busy.value = false
      await load()
    }
  } catch (cause) { if (current === generation && token === getToken()) failed(cause, '保存失败，请检查权限后重试。') }
  finally { if (current === generation) busy.value = false }
}
async function remove(item: EnterpriseContent) {
  if (!window.confirm(`确定删除“${item.title}”吗？`)) return
  const current = generation, id = props.enterpriseId, kind = props.kind
  const token = getToken()
  try {
    if (kind === 'forum') await deleteEnterprisePost(id, item.id, token)
    else await deleteEnterpriseAnnouncement(id, item.id, token)
    if (current === generation && token === getToken()) { selected.value = null; await load() }
  } catch (cause) { if (current === generation && token === getToken()) failed(cause, '删除失败，请检查权限后重试。') }
}
async function submitReply() {
  const postId = selected.value?.post.id, content = replyText.value.trim(), current = generation, id = props.enterpriseId
  const userId = props.currentUserId
  const token = getToken()
  const detail = detailGeneration
  if (!postId || !content || busy.value) return
  busy.value = true
  try {
    const request = await pendingKey(userId, id, `reply:${postId}`, [content])
    if (current !== generation || detail !== detailGeneration || token !== getToken()) return
    const created = await replyEnterprisePost(id, postId, content, request.id, token)
    clearPendingKey(request.storageKey)
    if (current === generation && detail === detailGeneration && token === getToken() && selected.value?.post.id === postId) {
      if (replyText.value.trim() === content) replyText.value = ''
      if (hasMoreReplies.value) notice.value = '回复已发表，继续加载回复可查看。'
      else if (!selected.value.replies.some(item => item.id === created.id)) selected.value.replies.push(created)
    }
  } catch (cause) { if (current === generation && token === getToken()) failed(cause, '回复失败，请重试。') }
  finally { if (current === generation) busy.value = false }
}
async function removeReply(reply: EnterpriseReply) {
  if (!window.confirm('确定删除这条回复吗？')) return
  const current = generation, detail = detailGeneration, id = props.enterpriseId
  const token = getToken()
  try {
    await deleteEnterpriseReply(id, reply.post_id, reply.id, token)
    if (current === generation && detail === detailGeneration && token === getToken() && selected.value?.post.id === reply.post_id) {
      await open(reply.post_id)
    }
  } catch (cause) { if (current === generation && token === getToken()) failed(cause, '删除回复失败，请重试。') }
}
watch(() => [props.enterpriseId, props.kind, props.currentUserId] as const, () => {
  ++generation; ++detailGeneration; blocked.value = false
  busy.value = false; pendingKeys.clear()
  items.value = []; selected.value = null; replyText.value = ''; notice.value = ''; resetForm()
  hasMoreItems.value = false; hasMoreReplies.value = false; pageLoading.value = false; replyLoading.value = false; void load()
}, { immediate: true })
onBeforeUnmount(() => { ++generation; ++detailGeneration })
</script>
<style scoped>
.community-card { background: #fff; border: 1px solid #e9eaf3; border-radius: 14px; padding: 20px; }
.head, .actions { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
h2 { font-size: 17px; margin: 0; } h3 { font-size: 15px; margin: 14px 0 8px; }
button, input, textarea { font: inherit; border: 1px solid #d9ddeb; background: #fff; border-radius: 8px; padding: 8px 10px; color: inherit; }
button { cursor: pointer; } button:disabled { opacity: .5; cursor: default; }.item { padding: 16px 0; border-bottom: 1px solid #edf0f6; }
.item-title { border: 0; padding: 0; font-size: 15px; font-weight: 600; color: #424cc5; text-align: left; }.meta, .muted { color: #69718a; font-size: 13px; }
.body { white-space: pre-wrap; word-break: break-word; line-height: 1.6; }.reply { border-top: 1px solid #edf0f6; padding: 10px 0; }
.editor, form { display: flex; flex-direction: column; gap: 8px; margin-top: 20px; } label { font-size: 13px; } textarea { resize: vertical; }
.actions { justify-content: flex-start; }.error { color: #a23434; }.detail > button { margin-bottom: 6px; }
</style>
