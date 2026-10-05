<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { View, ChatLineRound } from '@element-plus/icons-vue'
import { formatDate } from '@/utils/format'
import { getPosts, type ForumPost } from '@/api/forum'

const router = useRouter()

const CATEGORY: Record<string, string> = {
  qa: '问答', tech: '技术', share: '分享', announce: '公告', other: '其他',
}
const CATEGORY_TAG: Record<string, 'warning' | 'primary' | 'success' | 'danger' | 'info'> = {
  qa: 'warning', tech: 'primary', share: 'success', announce: 'danger', other: 'info',
}

const posts = ref<ForumPost[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(10)
const keyword = ref('')
const category = ref('')
const loading = ref(false)
const loadError = ref('')
const appliedFilters = ref({ keyword: '', category: '' })
const emptyDescription = computed(() => appliedFilters.value.keyword || appliedFilters.value.category
  ? '没有找到符合条件的帖子，试试其他关键词或分类'
  : '还没有帖子，来发第一帖吧')
let loadGeneration = 0

async function load() {
  const generation = ++loadGeneration
  const params = {
    page: page.value, page_size: pageSize.value,
    keyword: keyword.value.trim(), category: category.value,
  }
  loading.value = true
  loadError.value = ''
  posts.value = []
  total.value = 0
  try {
    const res = await getPosts(params)
    if (generation !== loadGeneration) return
    const lastPage = Math.max(1, Math.ceil(res.total / pageSize.value))
    if (page.value > lastPage) {
      page.value = lastPage
      await load()
      return
    }
    posts.value = res.items
    total.value = res.total
    appliedFilters.value = { keyword: params.keyword, category: params.category }
  } catch (error) {
    if (generation !== loadGeneration) return
    const message = (error as { message?: unknown })?.message
    loadError.value = typeof message === 'string' && message.trim() ? message : '帖子读取失败，请稍后重试'
  } finally {
    if (generation === loadGeneration) loading.value = false
  }
}

function search() {
  page.value = 1
  void load()
}

onMounted(load)
onBeforeUnmount(() => { loadGeneration++ })
</script>

<template>
  <div class="forum-page">
    <div class="page-header">
      <div>
        <h2>开发者论坛</h2>
        <p class="page-sub">提问、分享经验、交流审查实践</p>
      </div>
      <el-button type="primary" @click="router.push('/forum/new')">发布新帖</el-button>
    </div>

    <el-card shadow="never" class="filter-card">
      <div class="filter-row">
        <el-input v-model="keyword" aria-label="搜索帖子标题" placeholder="搜索标题" clearable class="search-input"
          @keyup.enter="search" @clear="search" />
        <el-select v-model="category" aria-label="帖子分类" placeholder="全部分类" clearable class="category-select"
          @change="search">
          <el-option v-for="(label, val) in CATEGORY" :key="val" :label="label" :value="val" />
        </el-select>
        <el-button @click="search">搜索</el-button>
      </div>
    </el-card>

    <el-card shadow="never">
      <div v-loading="loading" class="post-list" :aria-busy="loading">
        <div v-if="loadError" class="load-error" role="alert">
          <div><strong>帖子暂未读取</strong><p>{{ loadError }}</p></div>
          <el-button @click="load">重试</el-button>
        </div>
        <RouterLink v-for="p in posts" :key="p.id" :to="`/forum/${p.id}`" class="post-item">
          <div class="post-main">
            <div class="post-title">
              <el-tag v-if="p.is_pinned" type="danger" size="small" effect="dark">置顶</el-tag>
              <el-tag size="small" :type="CATEGORY_TAG[p.category] || 'info'">{{ CATEGORY[p.category] || p.category }}</el-tag>
              <span class="title-text">{{ p.title }}</span>
            </div>
            <div class="post-meta">
              <span>{{ p.author_name }}</span>
              <span>·</span>
              <span>{{ formatDate(p.create_time) }}</span>
            </div>
          </div>
          <div class="post-stats">
            <span :aria-label="`${p.view_count} 次浏览`"><el-icon aria-hidden="true"><View /></el-icon> {{ p.view_count }}</span>
            <span :aria-label="`${p.reply_count} 条回复`"><el-icon aria-hidden="true"><ChatLineRound /></el-icon> {{ p.reply_count }}</span>
          </div>
        </RouterLink>
        <el-empty v-if="!loading && !loadError && posts.length === 0" :description="emptyDescription" />
      </div>
      <div v-if="!loadError && total > 0" class="pager">
        <el-pagination layout="total, prev, pager, next" :pager-count="5" :disabled="loading" :total="total" :page-size="pageSize"
          :current-page="page" @current-change="(p: number) => { page = p; load() }" />
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.forum-page { min-width: 0; padding: 4px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-end; flex-wrap: wrap; gap: 16px; margin-bottom: 24px; }
.page-header > div { min-width: 0; }
.page-header h2 { margin: 0; font-size: 24px; line-height: 1.4; }
.page-sub { color: var(--el-text-color-secondary); margin: 8px 0 0; font-size: 14px; line-height: 1.6; }
.filter-card { margin-bottom: 16px; }
.filter-row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.search-input { flex: 1 1 240px; min-width: 0; }
.category-select { flex: 0 1 160px; min-width: 0; }
.forum-page :deep(.el-button) { min-height: 44px; }
.filter-row :deep(.el-input__wrapper), .filter-row :deep(.el-select__wrapper) { min-height: 44px; box-sizing: border-box; }
.post-list { min-height: 200px; }
.post-item {
  display: flex; justify-content: space-between; align-items: center; gap: 16px;
  padding: 16px 8px; border-bottom: 1px solid var(--el-border-color-lighter);
  color: var(--el-text-color-primary); text-decoration: none; border-radius: var(--r-md, 8px);
  transition: background 0.15s; min-width: 0;
}
.post-item:hover { background: var(--surface-hover, var(--el-fill-color-light)); }
.post-item:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: -2px; }
.post-main { flex: 1; min-width: 0; }
.post-title { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.title-text { font-weight: 600; font-size: 15px; line-height: 1.6; overflow-wrap: anywhere; }
.post-title :deep(.el-tag) { flex-shrink: 0; }
.post-meta { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; margin-top: 8px; display: flex; gap: 8px; flex-wrap: wrap; overflow-wrap: anywhere; }
.post-stats { display: flex; flex-shrink: 0; gap: 16px; color: var(--el-text-color-secondary); font-size: 13px; }
.post-stats span { display: flex; align-items: center; gap: 4px; white-space: nowrap; }
.pager { display: flex; justify-content: flex-end; margin-top: 16px; min-width: 0; }
.pager :deep(.el-pagination) { flex-wrap: wrap; gap: 8px 0; }
.load-error { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; padding: 16px; border-radius: var(--r-md, 8px); background: var(--el-color-danger-light-9); font-size: 14px; }
.load-error > div { min-width: 0; flex: 1 1 160px; }
.load-error p { margin: 8px 0 0; line-height: 1.6; overflow-wrap: anywhere; }
@media (max-width: 600px) {
  .forum-page { padding: 0; }
  .page-header { gap: 12px; margin-bottom: 16px; }
  .page-header h2 { font-size: 22px; }
  .category-select { flex: 1 1 140px; }
  .post-item { flex-direction: column; align-items: stretch; gap: 12px; padding-inline: 0; }
  .post-stats { align-self: flex-end; }
  .forum-page :deep(.el-card__body) { padding: 16px; }
  .pager { justify-content: center; }
}
</style>
