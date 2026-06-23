<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useUserStore } from '@/stores/user'
import { formatDate } from '@/utils/format'
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

const isAdmin = computed(() => userStore.profile?.role === 'admin')
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
  await ElMessageBox.confirm('确认删除该回复?', '提示', { type: 'warning' })
  await deleteReply(id)
  await load()
}

async function removePost() {
  await ElMessageBox.confirm('确认删除该帖子?', '提示', { type: 'warning' })
  await deletePost(postId)
  ElMessage.success('已删除')
  router.push('/forum')
}

async function togglePin() {
  if (!post.value) return
  await pinPost(postId, !post.value.is_pinned)
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
      <div class="post-body">{{ post.content }}</div>
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
        <div class="reply-body">{{ r.content }}</div>
      </div>
      <el-empty v-if="post.replies.length === 0" description="还没有回复" :image-size="80" />

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
.reply-card { margin-top: 16px; }
.block-title { margin: 0 0 12px; font-size: 15px; }
.reply-item { padding: 12px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.reply-head { display: flex; align-items: center; gap: 10px; }
.reply-author { font-weight: 600; }
.reply-time { color: var(--el-text-color-secondary); font-size: 12px; }
.reply-del { margin-left: auto; }
.reply-body { white-space: pre-wrap; line-height: 1.7; margin-top: 6px; }
.reply-editor { margin-top: 16px; }
.editor-actions { display: flex; justify-content: flex-end; margin-top: 10px; }
</style>
