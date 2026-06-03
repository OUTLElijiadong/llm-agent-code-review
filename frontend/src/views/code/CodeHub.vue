<template>
  <div class="code-hub-page">
    <div class="page-header">
      <div>
        <h2>代码中心</h2>
        <p class="page-sub">跨项目浏览所有代码文件，选择项目进入文件列表</p>
      </div>
      <el-input
        v-model="keyword"
        placeholder="按项目名搜索"
        clearable
        class="search-input"
        @input="handleSearch"
      >
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>
    </div>

    <el-alert
      v-if="!filteredProjects.length && !loading"
      title="还没有项目"
      description="先在「项目管理」创建项目并上传代码文件，这里会汇总展示。"
      type="info"
      :closable="false"
      show-icon
    />

    <div v-loading="loading" class="project-grid">
      <article
        v-for="proj in filteredProjects"
        :key="proj.id"
        class="project-card"
        @click="goProjectCode(proj)"
      >
        <header class="card-head">
          <span class="lang-tag">{{ proj.language || '未识别' }}</span>
          <el-tag v-if="proj.status === 'archived'" size="small" type="info">已归档</el-tag>
        </header>
        <h3 class="card-title">{{ proj.project_name }}</h3>
        <p class="card-desc">{{ proj.description || '无描述' }}</p>
        <footer class="card-foot">
          <span class="meta">文件 {{ proj.file_count ?? 0 }}</span>
          <span class="meta">最近审查 {{ formatDate(proj.last_review_at) }}</span>
        </footer>
      </article>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import dayjs from 'dayjs'
import { Search } from '@element-plus/icons-vue'
import { getProjects } from '@/api/project'
import type { ProjectOut } from '@/types/project'

const router = useRouter()
const loading = ref(false)
const projects = ref<ProjectOut[]>([])
const keyword = ref('')

const filteredProjects = computed(() => {
  if (!keyword.value.trim()) return projects.value
  const kw = keyword.value.trim().toLowerCase()
  return projects.value.filter((p) => p.project_name.toLowerCase().includes(kw))
})

function formatDate(time?: string): string {
  return time ? dayjs(time).format('YYYY-MM-DD HH:mm') : '-'
}

function goProjectCode(proj: ProjectOut): void {
  router.push(`/code/${proj.id}`)
}

function handleSearch(): void {
  /* 纯前端过滤，无需触发请求 */
}

async function loadProjects(): Promise<void> {
  loading.value = true
  try {
    const data = await getProjects({ page: 1, page_size: 100 })
    projects.value = data.items
  } finally {
    loading.value = false
  }
}

onMounted(loadProjects)
</script>

<style scoped lang="scss">
.code-hub-page {
  padding: var(--spacing-lg);
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  margin-bottom: var(--spacing-lg);
  gap: var(--spacing-md);
  flex-wrap: wrap;

  h2 {
    margin: 0 0 4px;
    font-size: 20px;
    font-weight: 600;
  }

  .page-sub {
    margin: 0;
    color: var(--color-text-secondary, #909399);
    font-size: 13px;
  }
}

.search-input {
  width: 240px;
}

.project-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: var(--spacing-md);
  margin-top: var(--spacing-md);
}

.project-card {
  padding: 18px;
  background: var(--color-bg-card, #fff);
  border: 1px solid var(--color-border-light, #ebeef5);
  border-radius: var(--border-radius-lg, 8px);
  cursor: pointer;
  transition: transform 0.15s, box-shadow 0.15s, border-color 0.15s;

  &:hover {
    transform: translateY(-2px);
    box-shadow: 0 8px 18px rgba(0, 0, 0, 0.08);
    border-color: var(--brand-300, #409eff);
  }
}

.card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;

  .lang-tag {
    font-size: 11px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--brand-500, #409eff);
    font-weight: 600;
  }
}

.card-title {
  margin: 0 0 6px;
  font-size: 15px;
  font-weight: 600;
  color: var(--color-text-primary, #303133);
}

.card-desc {
  margin: 0 0 14px;
  font-size: 13px;
  color: var(--color-text-secondary, #909399);
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.card-foot {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: var(--color-text-secondary, #909399);
}
</style>
