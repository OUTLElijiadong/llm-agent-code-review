<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { View, ChatLineRound } from '@element-plus/icons-vue'
import { formatDate } from '@/utils/format'
import { getPosts, type ForumPost } from '@/api/forum'

const router = useRouter()

const CATEGORY: Record<string, string> = {
  qa: '问答', tech: '技术', share: '分享', announce: '公告', other: '其他',
}
const CATEGORY_TAG: Record<string, string> = {
  qa: 'warning', tech: 'primary', share: 'success', announce: 'danger', other: 'info',
}

const posts = ref<ForumPost[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(10)
const keyword = ref('')
const category = ref('')
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const res = await getPosts({
      page: page.value, page_size: pageSize.value,
      keyword: keyword.value, category: category.value,
    })
    posts.value = res.items
    total.value = res.total
  } finally {
    loading.value = false
  }
}

function openPost(id: number) {
  router.push(`/forum/${id}`)
}

onMounted(load)
</script>

<template>
  <div class="forum-page">
    <div class="page-header">
      <div>
        <h2>开发者论坛</h2>
        <p class="page-sub">提问、分享经验、交流最佳实践 —— 全员可发帖</p>
      </div>
      <el-button type="primary" @click="router.push('/forum/new')">发布新帖</el-button>
    </div>

    <el-card shadow="never" class="filter-card">
      <div class="filter-row">
        <el-input v-model="keyword" placeholder="搜索标题" clearable style="width: 240px"
          @keyup.enter="() => { page = 1; load() }" />
        <el-select v-model="category" placeholder="全部分类" clearable style="width: 140px"
          @change="() => { page = 1; load() }">
          <el-option v-for="(label, val) in CATEGORY" :key="val" :label="label" :value="val" />
        </el-select>
        <el-button @click="() => { page = 1; load() }">搜索</el-button>
      </div>
    </el-card>

    <el-card shadow="never">
      <div v-loading="loading" class="post-list">
        <div v-for="p in posts" :key="p.id" class="post-item" @click="openPost(p.id)">
          <div class="post-main">
            <div class="post-title">
              <el-tag v-if="p.is_pinned" type="danger" size="small" effect="dark">置顶</el-tag>
              <el-tag size="small" :type="CATEGORY_TAG[p.category] as any">{{ CATEGORY[p.category] }}</el-tag>
              <span class="title-text">{{ p.title }}</span>
            </div>
            <div class="post-meta">
              <span>{{ p.author_name }}</span>
              <span>·</span>
              <span>{{ formatDate(p.create_time) }}</span>
            </div>
          </div>
          <div class="post-stats">
            <span><el-icon><View /></el-icon> {{ p.view_count }}</span>
            <span><el-icon><ChatLineRound /></el-icon> {{ p.reply_count }}</span>
          </div>
        </div>
        <el-empty v-if="!loading && posts.length === 0" description="还没有帖子,来发第一帖吧" />
      </div>
      <div class="pager">
        <el-pagination layout="total, prev, pager, next" :total="total" :page-size="pageSize"
          :current-page="page" @current-change="(p: number) => { page = p; load() }" />
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.forum-page { padding: 4px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 16px; }
.page-header h2 { margin: 0; }
.page-sub { color: var(--el-text-color-secondary); margin: 4px 0 0; font-size: 13px; }
.filter-card { margin-bottom: 12px; }
.filter-row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.post-list { min-height: 200px; }
.post-item {
  display: flex; justify-content: space-between; align-items: center;
  padding: 14px 8px; border-bottom: 1px solid var(--el-border-color-lighter); cursor: pointer;
  transition: background 0.15s;
}
.post-item:hover { background: var(--el-fill-color-light); }
.post-title { display: flex; align-items: center; gap: 8px; }
.title-text { font-weight: 600; font-size: 15px; }
.post-meta { color: var(--el-text-color-secondary); font-size: 12px; margin-top: 6px; display: flex; gap: 6px; }
.post-stats { display: flex; gap: 18px; color: var(--el-text-color-secondary); font-size: 13px; }
.post-stats span { display: flex; align-items: center; gap: 4px; }
.pager { display: flex; justify-content: flex-end; margin-top: 12px; }
</style>
