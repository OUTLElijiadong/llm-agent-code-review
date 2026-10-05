<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useUserStore } from '@/stores/user'
import { formatDate } from '@/utils/format'
import { renderMarkdown } from '@/utils/markdown'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { ElMessage } from 'element-plus/es/components/message/index'
import {
  getPost, createReply, deletePost, deleteReply, pinPost, type ForumPostDetail,
} from '@/api/forum'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const postId = Number(route.params.id)

const CATEGORY: Record<string, string> = {
  qa: '问答', tech: '技术', share: '分享', announce: '公告', other: '其他',
}

const post = ref<ForumPostDetail | null>(null)
const loading = ref(false)
const replyContent = ref('')
const replying = ref(false)
const loadError = ref('')
const replyError = ref('')
const actionError = ref('')
const actionBusy = ref(false)
let loadGeneration = 0
let hasLoadedPost = false

const isAdmin = computed(() => userStore.isAdmin())
const myId = computed(() => userStore.profile?.id)
const isAuthor = computed(() => post.value?.user_id === myId.value)
const canEdit = computed(() => isAuthor.value || isAdmin.value)

function errorMessage(error: unknown, fallback: string): string {
  const message = (error as { message?: unknown })?.message
  return typeof message === 'string' && message.trim() ? message : fallback
}

function isCancelled(error: unknown): boolean {
  return error === 'cancel' || error === 'close'
}

async function load() {
  const generation = ++loadGeneration
  loading.value = true
  loadError.value = ''
  try {
    const data = await getPost(postId, { recordView: !hasLoadedPost })
    if (generation !== loadGeneration) return
    post.value = data
    hasLoadedPost = true
  } catch (error) {
    if (generation === loadGeneration) loadError.value = errorMessage(error, '帖子读取失败，请稍后重试')
  } finally {
    if (generation === loadGeneration) loading.value = false
  }
}

async function submitReply() {
  if (replying.value || loading.value || !post.value) return
  if (!replyContent.value.trim()) {
    ElMessage.warning('回复内容不能为空')
    return
  }
  replying.value = true
  replyError.value = ''
  try {
    await createReply(postId, replyContent.value)
    replyContent.value = ''
    ElMessage.success('回复成功')
    await load()
  } catch (error) {
    replyError.value = errorMessage(error, '回复未能保存，当前草稿已保留')
  } finally {
    replying.value = false
  }
}

async function removeReply(id: number) {
  const reply = post.value?.replies.find(item => item.id === id)
  if (actionBusy.value || loading.value || !reply || (reply.user_id !== myId.value && !isAdmin.value)) return
  actionBusy.value = true
  actionError.value = ''
  try {
    await ElMessageBox.confirm('确认删除该回复？', '删除回复', { type: 'warning' })
    await deleteReply(id)
    ElMessage.success('回复已删除')
    await load()
  } catch (error) {
    if (!isCancelled(error)) actionError.value = errorMessage(error, '删除回复失败，请重试该操作')
  } finally {
    actionBusy.value = false
  }
}

async function removePost() {
  if (actionBusy.value || loading.value || !canEdit.value) return
  actionBusy.value = true
  actionError.value = ''
  try {
    await ElMessageBox.confirm('确认删除该帖子？', '删除帖子', { type: 'warning' })
    await deletePost(postId)
    ElMessage.success('已删除')
    await router.push('/forum')
  } catch (error) {
    if (!isCancelled(error)) actionError.value = errorMessage(error, '删除帖子失败，请重试该操作')
  } finally {
    actionBusy.value = false
  }
}

async function togglePin() {
  if (!post.value || !isAdmin.value || actionBusy.value || loading.value) return
  const target = !post.value.is_pinned
  actionBusy.value = true
  actionError.value = ''
  try {
    await pinPost(postId, target)
    ElMessage.success(target ? '已置顶' : '已取消置顶')
    await load()
  } catch (error) {
    actionError.value = errorMessage(error, '置顶状态未更新，请重试该操作')
  } finally {
    actionBusy.value = false
  }
}

onMounted(load)
onBeforeUnmount(() => { loadGeneration++ })
</script>

<template>
  <div v-loading="loading" class="post-detail-page" :aria-busy="loading">
    <el-button link @click="router.push('/forum')">← 返回论坛</el-button>
    <div v-if="loadError" class="error-panel" role="alert">
      <div><strong>{{ post ? '帖子刷新未完成' : '帖子暂未读取' }}</strong><p>{{ loadError }}</p><p v-if="post">当前显示上次读取的内容，重试后更新。</p></div>
      <el-button @click="load">重试</el-button>
    </div>
    <div v-if="actionError" class="error-panel" role="alert">
      <div><strong>操作未完成</strong><p>{{ actionError }}</p><p>可再次点击对应操作重试。</p></div>
    </div>

    <el-card v-if="post" shadow="never" class="main-card">
      <div class="post-head">
        <div class="title-line">
          <el-tag v-if="post.is_pinned" type="danger" size="small" effect="dark">置顶</el-tag>
          <el-tag size="small">{{ CATEGORY[post.category] || post.category }}</el-tag>
          <h2>{{ post.title }}</h2>
        </div>
        <div class="actions">
          <el-button v-if="isAdmin" link type="warning" :disabled="actionBusy || loading" @click="togglePin">
            {{ post.is_pinned ? '取消置顶' : '置顶' }}
          </el-button>
          <el-button v-if="canEdit" link type="primary" :disabled="actionBusy || loading"
            @click="router.push(`/forum/${post.id}/edit`)">编辑</el-button>
          <el-button v-if="canEdit" link type="danger" :disabled="actionBusy || loading" @click="removePost">删除</el-button>
        </div>
      </div>
      <div class="post-meta">
        <span>{{ post.author_name }}</span>
        <span>·</span>
        <span>{{ formatDate(post.create_time) }}</span>
        <span>·</span>
        <span>{{ post.view_count }} 浏览</span>
      </div>
      <div class="post-body md-body" v-html="renderMarkdown(post.content)"></div>
    </el-card>

    <el-card v-if="post" shadow="never" class="reply-card">
      <h3 class="block-title">全部回复 ({{ post.replies.length }})</h3>
      <div v-for="r in post.replies" :key="r.id" class="reply-item">
        <div class="reply-head">
          <span class="reply-author">{{ r.author_name }}</span>
          <span class="reply-time">{{ formatDate(r.create_time) }}</span>
          <el-button v-if="r.user_id === myId || isAdmin" link type="danger" :disabled="actionBusy || loading"
            class="reply-del" @click="removeReply(r.id)">删除</el-button>
        </div>
        <div class="reply-body md-body" v-html="renderMarkdown(r.content)"></div>
      </div>
      <el-empty v-if="post.replies.length === 0" description="还没有回复" :image-size="80" />

      <div class="reply-editor">
        <div v-if="replyError" class="error-panel" role="alert">
          <div><strong>回复草稿已保留</strong><p>{{ replyError }}</p></div>
          <el-button :disabled="replying || loading" @click="submitReply">重试回复</el-button>
        </div>
        <el-input v-model="replyContent" aria-label="回复内容" :disabled="replying" type="textarea" :rows="4" maxlength="50000" placeholder="写下你的回复…" />
        <div class="editor-actions">
          <el-button type="primary" :loading="replying" :disabled="loading" @click="submitReply">发表回复</el-button>
        </div>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.post-detail-page { padding: 4px; min-width: 0; }
.post-detail-page :deep(.el-button) { min-height: 44px; }
.main-card { margin-top: 12px; }
.post-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.title-line { display: flex; flex: 1; min-width: 0; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.title-line h2 { margin: 0; font-size: 22px; line-height: 1.5; overflow-wrap: anywhere; }
.title-line :deep(.el-tag) { flex-shrink: 0; }
.actions { display: flex; align-items: flex-end; gap: 12px; flex-shrink: 0; flex-wrap: wrap; }
.actions :deep(.el-button + .el-button) { margin-left: 0; }
.post-meta { color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.6; margin: 12px 0 16px; display: flex; gap: 8px; flex-wrap: wrap; overflow-wrap: anywhere; }
.post-body {
  white-space: pre-wrap; line-height: 1.8; font-size: 15px;
  border-top: 1px solid var(--el-border-color-lighter); padding-top: 16px;
}
/* Markdown 渲染:帖子/回复经 renderMarkdown 输出 HTML,补齐元素间距与排版 */
.md-body { white-space: normal; overflow-wrap: anywhere; overflow-x: auto; min-width: 0; }
.md-body :deep(img) { max-width: 100%; height: auto; }
.md-body :deep(h1), .md-body :deep(h2), .md-body :deep(h3),
.md-body :deep(h4), .md-body :deep(h5), .md-body :deep(h6) {
  margin: 14px 0 8px; line-height: 1.4; font-weight: 700;
}
.md-body :deep(h2) { font-size: 17px; }
.md-body :deep(h3) { font-size: 15px; }
.md-body :deep(p) { margin: 8px 0; }
.md-body :deep(ul), .md-body :deep(ol) { margin: 8px 0; padding-left: 22px; }
.md-body :deep(li) { margin: 3px 0; }
.md-body :deep(code) {
  background: var(--el-fill-color-light); border-radius: 4px;
  padding: 1px 5px; font-size: 13px; font-family: ui-monospace, monospace;
}
.md-body :deep(pre) {
  background: var(--el-fill-color-light); border-radius: 6px;
  padding: 12px; overflow-x: auto; max-width: 100%; box-sizing: border-box; margin: 8px 0;
}
.md-body :deep(pre code) { background: none; padding: 0; }
.md-body :deep(blockquote) {
  margin: 8px 0; padding: 4px 12px; color: var(--el-text-color-secondary);
  border-left: 3px solid var(--el-border-color);
}
.md-body :deep(a) { color: var(--el-color-primary); text-decoration: none; }
.md-body :deep(a:hover) { text-decoration: underline; }
.md-body :deep(hr) { border: none; border-top: 1px solid var(--el-border-color-lighter); margin: 14px 0; }
.md-body :deep(table) { border-collapse: collapse; margin: 8px 0; }
.md-body :deep(th), .md-body :deep(td) {
  border: 1px solid var(--el-border-color-lighter); padding: 5px 10px; font-size: 14px;
}
.reply-card { margin-top: 16px; }
.block-title { margin: 0 0 12px; font-size: 15px; }
.reply-item { padding: 12px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.reply-head { display: flex; align-items: baseline; gap: 8px 12px; flex-wrap: wrap; }
.reply-author { font-weight: 600; overflow-wrap: anywhere; min-width: 0; }
.reply-time { color: var(--el-text-color-secondary); font-size: 12px; }
.reply-del { margin-left: auto; }
.reply-body { white-space: pre-wrap; line-height: 1.7; margin-top: 6px; }
.reply-body.md-body { white-space: normal; }
.reply-editor { margin-top: 16px; }
.editor-actions { display: flex; justify-content: flex-end; margin-top: 10px; }
.error-panel { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 16px; padding: 16px; margin: 16px 0; border-radius: var(--r-md, 8px); background: var(--el-color-danger-light-9); font-size: 14px; }
.error-panel > div { min-width: 0; flex: 1 1 160px; }
.error-panel p { margin: 8px 0 0; line-height: 1.6; overflow-wrap: anywhere; }
@media (max-width: 600px) {
  .post-detail-page { padding: 0; }
  .post-head { flex-direction: column; gap: 8px; }
  .actions { width: 100%; justify-content: flex-end; }
  .title-line h2 { font-size: 20px; }
  .post-detail-page :deep(.el-card__body) { padding: 16px; }
}
</style>
