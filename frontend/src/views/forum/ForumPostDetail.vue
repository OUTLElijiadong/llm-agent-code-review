<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useUserStore } from '@/stores/user'
import { formatDate } from '@/utils/format'
import { renderMarkdown } from '@/utils/markdown'
import { ElMessage } from 'element-plus/es/components/message/index'
import { confirmDanger } from '@/composables/useDangerConfirm'
import EmptyState from '@/components/common/EmptyState.vue'
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

const isAdmin = computed(() => userStore.isAdmin())
const myId = computed(() => userStore.profile?.id)
const isAuthor = computed(() => post.value?.user_id === myId.value)

async function load() {
  loading.value = true
  try {
    post.value = await getPost(postId)
  } finally {
    loading.value = false
  }
}

async function submitReply() {
  if (!replyContent.value.trim()) {
    ElMessage.warning('回复内容不能为空')
    return
  }
  replying.value = true
  try {
    await createReply(postId, replyContent.value)
    replyContent.value = ''
    await load()
    ElMessage.success('回复成功')
  } finally {
    replying.value = false
  }
}

async function removeReply(id: number) {
  if (!await confirmDanger({ target: '删除该回复' })) return
  await deleteReply(id)
  await load()
}

async function removePost() {
  if (!await confirmDanger({ target: '删除该帖子', extra: '帖子下的所有回复会一并删除' })) return
  await deletePost(postId)
  ElMessage.success('已删除')
  router.push('/forum')
}

async function togglePin() {
  if (!post.value) return
  const target = !post.value.is_pinned
  await pinPost(postId, target)
  ElMessage.success(target ? '已置顶' : '已取消置顶')
  await load()
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="post-detail-page">
    <el-button link @click="router.push('/forum')">← 返回论坛</el-button>

    <el-card v-if="post" shadow="never" class="main-card">
      <div class="post-head">
        <div class="title-line">
          <el-tag v-if="post.is_pinned" type="danger" size="small" effect="dark">置顶</el-tag>
          <el-tag size="small">{{ CATEGORY[post.category] || post.category }}</el-tag>
          <h2>{{ post.title }}</h2>
        </div>
        <div class="actions">
          <el-button v-if="isAdmin" link type="warning" size="small" @click="togglePin">
            {{ post.is_pinned ? '取消置顶' : '置顶' }}
          </el-button>
          <el-button v-if="isAuthor" link type="primary" size="small"
            @click="router.push(`/forum/${post.id}/edit`)">编辑</el-button>
          <el-button v-if="isAuthor || isAdmin" link type="danger" size="small" @click="removePost">删除</el-button>
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
          <el-button v-if="r.user_id === myId || isAdmin" link type="danger" size="small"
            class="reply-del" @click="removeReply(r.id)">删除</el-button>
        </div>
        <div class="reply-body md-body" v-html="renderMarkdown(r.content)"></div>
      </div>
      <EmptyState v-if="post.replies.length === 0" description="还没有回复,来抢沙发" compact />

      <div class="reply-editor">
        <el-input v-model="replyContent" type="textarea" :rows="3" placeholder="写下你的回复…" />
        <div class="editor-actions">
          <el-button type="primary" :loading="replying" @click="submitReply">发表回复</el-button>
        </div>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.post-detail-page { padding: 4px; }
.main-card { margin-top: 12px; }
.post-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.title-line { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.title-line h2 { margin: 0; font-size: 20px; }
.post-meta { color: var(--el-text-color-secondary); font-size: 13px; margin: 10px 0 16px; display: flex; gap: 8px; }
.post-body {
  white-space: pre-wrap; line-height: 1.8; font-size: 15px;
  border-top: 1px solid var(--el-border-color-lighter); padding-top: 16px;
}
/* Markdown 渲染:帖子/回复经 renderMarkdown 输出 HTML,补齐元素间距与排版 */
.md-body { white-space: normal; }
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
  padding: 10px 12px; overflow-x: auto; margin: 8px 0;
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
.reply-head { display: flex; align-items: center; gap: 10px; }
.reply-author { font-weight: 600; }
.reply-time { color: var(--el-text-color-secondary); font-size: 12px; }
.reply-del { margin-left: auto; }
.reply-body { white-space: pre-wrap; line-height: 1.7; margin-top: 6px; }
.reply-body.md-body { white-space: normal; }
.reply-editor { margin-top: 16px; }
.editor-actions { display: flex; justify-content: flex-end; margin-top: 10px; }
</style>
