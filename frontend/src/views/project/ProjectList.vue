<template>
  <div class="project-list">
    <!-- ============ 页头 ============ -->
    <header class="page-head">
      <div>
        <h1 class="page-title font-display">项目管理</h1>
        <p class="page-sub">共 <b class="hl">{{ total }}</b> 个项目 · {{ activeCount }} 活跃 · {{ archivedCount }} 归档</p>
      </div>
      <div class="page-actions">
        <div class="view-switch">
          <button
            class="view-btn"
            :class="{ active: view === 'table' }"
            @click="view = 'table'"
          >
            <span class="ico">☰</span><span>表格</span>
          </button>
          <button
            class="view-btn"
            :class="{ active: view === 'card' }"
            @click="view = 'card'"
          >
            <span class="ico">▦</span><span>卡片</span>
          </button>
        </div>
        <el-button type="primary" :icon="Plus" @click="handleCreate">新建项目</el-button>
      </div>
    </header>

    <!-- ============ 筛选条 ============ -->
    <section class="filter-bar">
      <el-input
        v-model="keyword"
        placeholder="搜索项目名称或描述"
        clearable
        :prefix-icon="Search"
        class="search-input"
        @keyup.enter="handleSearch"
        @clear="handleSearch"
      />
      <el-select v-model="languageFilter" placeholder="语言" clearable class="filter-select" @change="handleSearch">
        <el-option label="Python" value="python" />
        <el-option label="JavaScript" value="javascript" />
        <el-option label="TypeScript" value="typescript" />
        <el-option label="Java" value="java" />
        <el-option label="Go" value="go" />
        <el-option label="C++" value="cpp" />
      </el-select>
      <el-select v-model="statusFilter" placeholder="状态" clearable class="filter-select" @change="handleSearch">
        <el-option label="活跃" value="active" />
        <el-option label="归档" value="archived" />
      </el-select>
      <el-button @click="handleReset">重置</el-button>
      <div class="filter-spacer"></div>
      <span class="filter-result font-mono">{{ projects.length }} / {{ total }} 条</span>
    </section>

    <!-- ============ 表格视图 ============ -->
    <section v-show="view === 'table'" class="table-card" v-loading="loading">
      <table class="prism-table">
        <thead>
          <tr>
            <th class="col-name">项目名称</th>
            <th class="col-lang">语言</th>
            <th class="col-status">状态</th>
            <th class="col-score">评分</th>
            <th class="col-files">文件</th>
            <th class="col-last">最近审查</th>
            <th class="col-create">创建时间</th>
            <th class="col-act">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in projects" :key="row.id" @click="handleView(row)">
            <td>
              <div class="cell-name">
                <span class="proj-avatar" :style="{ background: languageColor(row.language) }">
                  {{ initials(row.project_name) }}
                </span>
                <div class="proj-meta">
                  <span class="proj-name">{{ row.project_name }}</span>
                  <span class="proj-desc">{{ row.description || '—' }}</span>
                </div>
              </div>
            </td>
            <td>
              <span v-if="row.language" class="lang-chip font-mono">{{ row.language }}</span>
              <span v-else class="muted">-</span>
            </td>
            <td>
              <span class="status-pill" :class="`s-${row.status}`">
                <span class="pill-dot"></span>{{ row.status === 'active' ? '活跃' : '归档' }}
              </span>
            </td>
            <td>
              <div v-if="hasRealScore(row)" class="mini-gauge" :title="`评分 ${displayScore(row)}`">
                <svg viewBox="0 0 36 36" class="gauge-svg">
                  <circle cx="18" cy="18" r="14" fill="none" stroke="var(--gray-100)" stroke-width="3"/>
                  <circle
                    cx="18" cy="18" r="14" fill="none"
                    :stroke="scoreColor(displayScore(row))" stroke-width="3"
                    stroke-linecap="round"
                    :stroke-dasharray="`${displayScore(row) * 0.88} 100`"
                    transform="rotate(-90 18 18)"
                  />
                </svg>
                <span class="gauge-text font-mono" :style="{ color: scoreColor(displayScore(row)) }">
                  {{ displayScore(row) }}
                </span>
              </div>
              <span v-else class="muted font-mono" title="尚未审查或后端未返回评分">—</span>
            </td>
            <td>
              <span class="file-count font-mono">{{ row.file_count }}</span>
            </td>
            <td>
              <span v-if="row.last_review_at" class="font-mono muted-2">{{ formatDate(row.last_review_at) }}</span>
              <span v-else class="muted font-mono">暂未审查</span>
            </td>
            <td>
              <span class="font-mono muted-2">{{ formatDate(row.create_time) }}</span>
            </td>
            <td class="col-act" @click.stop>
              <el-button link type="primary" @click="handleView(row)">详情</el-button>
              <el-button link type="primary" @click="handleEdit(row)">编辑</el-button>
              <el-button link type="danger" @click="handleDelete(row.id)">删除</el-button>
            </td>
          </tr>
          <tr v-if="!loading && projects.length === 0">
            <td colspan="8">
              <EmptyState description="还没有项目，点击右上角新建一个吧">
                <el-button type="primary" @click="handleCreate">+ 新建项目</el-button>
              </EmptyState>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- ============ 卡片视图 ============ -->
    <section v-show="view === 'card'" class="card-grid" v-loading="loading">
      <article
        v-for="row in projects"
        :key="row.id"
        class="proj-card"
        @click="handleView(row)"
      >
        <header class="proj-card-head">
          <span class="proj-avatar lg" :style="{ background: languageColor(row.language) }">
            {{ initials(row.project_name) }}
          </span>
          <div class="head-meta">
            <div class="card-name">{{ row.project_name }}</div>
            <div class="card-sub font-mono">{{ row.language || 'unknown' }} · {{ row.file_count }} files</div>
          </div>
          <span class="status-pill" :class="`s-${row.status}`">
            <span class="pill-dot"></span>{{ row.status === 'active' ? '活跃' : '归档' }}
          </span>
        </header>

        <p class="card-desc">{{ row.description || '暂无描述' }}</p>

        <div class="card-spectrum spectrum-bar">
          <div></div><div></div><div></div><div></div>
          <div></div><div></div><div></div><div></div>
        </div>

        <footer class="card-foot">
          <div class="foot-meta">
            <span class="font-mono">{{ formatDate(row.last_review_at) || '暂未审查' }}</span>
          </div>
          <div class="mini-gauge sm">
            <svg viewBox="0 0 36 36" class="gauge-svg">
              <circle cx="18" cy="18" r="14" fill="none" stroke="var(--gray-100)" stroke-width="3"/>
              <circle
                cx="18" cy="18" r="14" fill="none"
                :stroke="scoreColor(displayScore(row))" stroke-width="3"
                stroke-linecap="round"
                :stroke-dasharray="`${displayScore(row) * 0.88} 100`"
                transform="rotate(-90 18 18)"
              />
            </svg>
            <span class="gauge-text font-mono" :style="{ color: scoreColor(displayScore(row)) }">
              {{ displayScore(row) }}
            </span>
          </div>
        </footer>
      </article>

      <div v-if="!loading && projects.length === 0" class="card-empty">
        <EmptyState description="还没有项目">
          <el-button type="primary" @click="handleCreate">+ 新建项目</el-button>
        </EmptyState>
      </div>
    </section>

    <!-- ============ 分页 ============ -->
    <div v-if="total > 0" class="pagination-wrap">
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="total"
        :page-sizes="[10, 20, 50]"
        layout="total, sizes, prev, pager, next, jumper"
        @size-change="fetchProjects"
        @current-change="fetchProjects"
      />
    </div>

    <ProjectForm
      v-model:visible="formVisible"
      :mode="formMode"
      :initial-data="editingProject"
      @submit="onFormSubmit"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { Plus, Search } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import dayjs from 'dayjs'
import { getProjects, deleteProject, createProject, updateProject } from '@/api/project'
import { uploadFolder } from '@/api/codeFile'
import type { ProjectOut } from '@/types/project'
import ProjectForm from './ProjectForm.vue'
import EmptyState from '@/components/common/EmptyState.vue'

const router = useRouter()

const view = ref<'table' | 'card'>('table')

const loading = ref(false)
const projects = ref<ProjectOut[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(10)
const keyword = ref('')
const languageFilter = ref('')
const statusFilter = ref('')

const formVisible = ref(false)
const formMode = ref<'create' | 'edit'>('create')
const editingProject = ref<ProjectOut | null>(null)

const activeCount = computed(() => projects.value.filter((p) => p.status === 'active').length)
const archivedCount = computed(() => projects.value.filter((p) => p.status === 'archived').length)

function formatDate(dateStr?: string): string {
  if (!dateStr) return ''
  return dayjs(dateStr).format('YYYY-MM-DD HH:mm')
}

function initials(name: string): string {
  if (!name) return '·'
  const m = name.match(/[A-Za-z一-鿿]/)
  return (m ? m[0] : name[0]).toUpperCase()
}

const langPalette: Record<string, string> = {
  python:     'linear-gradient(135deg,#4B9BFF,#2BBFB9)',
  javascript: 'linear-gradient(135deg,#D4A53A,#E08648)',
  typescript: 'linear-gradient(135deg,#4B9BFF,#5B58E8)',
  java:       'linear-gradient(135deg,#E27C4A,#E25C73)',
  go:         'linear-gradient(135deg,#2BBFB9,#4FB87A)',
  cpp:        'linear-gradient(135deg,#B85AC4,#5B58E8)',
}

function languageColor(lang?: string): string {
  if (!lang) return 'linear-gradient(135deg,#6E7689,#9BA3B0)'
  return langPalette[lang.toLowerCase()] ?? 'linear-gradient(135deg,#5B58E8,#8E88F5)'
}

function displayScore(row: ProjectOut): number {
  // v2.0: 必须来自后端真实评分,不再用 id hash 派生假数字
  if (typeof row.score === 'number') return Math.round(row.score)
  return 0
}

function hasRealScore(row: ProjectOut): boolean {
  return typeof row.score === 'number'
}

function scoreColor(score: number): string {
  if (score === 0) return 'var(--gray-300)'
  if (score >= 85) return 'var(--status-fixed)'
  if (score >= 70) return 'var(--sev-medium)'
  if (score >= 60) return 'var(--sev-high)'
  return 'var(--sev-severe)'
}

async function fetchProjects(): Promise<void> {
  loading.value = true
  try {
    const params: Record<string, unknown> = {
      page: page.value,
      page_size: pageSize.value,
    }
    if (keyword.value) params.keyword = keyword.value
    if (languageFilter.value) params.language = languageFilter.value
    if (statusFilter.value) params.status = statusFilter.value

    const res = await getProjects(params)
    projects.value = res.items
    total.value = res.total
  } catch {
    projects.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function handleSearch(): void {
  page.value = 1
  fetchProjects()
}

function handleReset(): void {
  keyword.value = ''
  languageFilter.value = ''
  statusFilter.value = ''
  handleSearch()
}

function handleCreate(): void {
  formMode.value = 'create'
  editingProject.value = null
  formVisible.value = true
}

function handleEdit(row: ProjectOut): void {
  formMode.value = 'edit'
  editingProject.value = row
  formVisible.value = true
}

function handleView(row: ProjectOut): void {
  router.push(`/projects/${row.id}`)
}

async function handleDelete(id: number): Promise<void> {
  try {
    await ElMessageBox.confirm('确定删除该项目吗？删除后不可恢复', '删除项目', {
      confirmButtonText: '确定删除',
      cancelButtonText: '取消',
      type: 'warning',
      confirmButtonClass: 'el-button--danger',
    })
    await deleteProject(id)
    ElMessage.success('项目已删除')
    if (projects.value.length === 1 && page.value > 1) {
      page.value--
    }
    await fetchProjects()
  } catch {
    /* 用户取消或 http 拦截器已处理 */
  }
}

async function onFormSubmit(data: { project_name: string; description?: string; language?: string; files?: File[] }): Promise<void> {
  if (formMode.value === 'create') {
    const { files, ...projectData } = data
    const result = await createProject(projectData)
    ElMessage.success('项目创建成功')
    if (files && files.length > 0) {
      ElMessage.info(`正在上传 ${files.length} 个文件...`)
      try {
        const uploadResult = await uploadFolder(result.id, files)
        if (uploadResult.success_count > 0) {
          ElMessage.success(`成功上传 ${uploadResult.success_count} 个文件`)
        }
        if (uploadResult.fail_count > 0) {
          const errMsg = uploadResult.errors.slice(0, 3).map((e: any) => e.error).join('; ')
          ElMessage.warning({ message: `${uploadResult.fail_count} 个文件上传失败: ${errMsg}`, duration: 6000 })
        }
      } catch (e: any) {
        const detail = e?.response?.data?.detail || e?.message || e?.toString() || ''
        ElMessage.error({ message: `文件上传失败: ${detail}`, duration: 6000 })
      }
    }
  } else if (editingProject.value) {
    await updateProject(editingProject.value.id, data)
    ElMessage.success('项目更新成功')
  }
  formVisible.value = false
  await fetchProjects()
}

onMounted(() => {
  fetchProjects()
})
</script>

<style scoped lang="scss">
.project-list {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* ============ 页头 ============ */
.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 16px;
}

.page-title {
  font-size: 26px;
  font-weight: 600;
  letter-spacing: -0.015em;
  color: var(--gray-900);
  margin: 0;
}

.page-sub {
  margin-top: 4px;
  font-size: 13px;
  color: var(--gray-500);

  .hl { color: var(--brand-600); font-weight: 600; }
}

.page-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.view-switch {
  display: inline-flex;
  padding: 4px;
  background: var(--gray-100);
  border-radius: 10px;
}

.view-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 14px;
  border: none;
  background: transparent;
  border-radius: 7px;
  font-size: 12.5px;
  color: var(--gray-500);
  cursor: pointer;
  transition: all 0.15s ease;

  &:hover { color: var(--gray-900); }

  &.active {
    background: #fff;
    color: var(--gray-900);
    font-weight: 500;
    box-shadow: var(--shadow-1);
  }

  .ico { font-family: var(--font-mono); font-size: 13px; }
}

/* ============ 筛选条 ============ */
.filter-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 16px;
  background: #fff;
  border: 1px solid var(--gray-100);
  border-radius: 12px;
}

.search-input { width: 280px; }
.filter-select { width: 140px; }
.filter-spacer { flex: 1; }
.filter-result {
  font-size: 11.5px;
  color: var(--gray-500);
}

/* ============ 表格 ============ */
.table-card {
  background: #fff;
  border: 1px solid var(--gray-100);
  border-radius: 12px;
  overflow: hidden;
  box-shadow: var(--shadow-1);
}

.prism-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;

  thead th {
    text-align: left;
    padding: 14px 18px;
    background: var(--gray-50);
    color: var(--gray-500);
    font-weight: 500;
    font-size: 12px;
    letter-spacing: 0.02em;
    border-bottom: 1px solid var(--gray-100);
  }

  tbody tr {
    cursor: pointer;
    transition: background 0.15s ease;

    &:hover { background: var(--brand-50); }
    &:not(:last-child) td { border-bottom: 1px solid var(--gray-100); }
  }

  tbody td {
    padding: 14px 18px;
    color: var(--gray-700);
    vertical-align: middle;
  }

  .col-act { white-space: nowrap; text-align: right; }
  .col-score { width: 90px; }
  .col-files { width: 80px; }
  .col-status { width: 100px; }
  .col-lang { width: 110px; }
  .col-last, .col-create { width: 160px; }
}

.cell-name {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.proj-avatar {
  width: 36px;
  height: 36px;
  border-radius: 9px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-family: var(--font-display);
  font-size: 14px;
  font-weight: 600;
  flex-shrink: 0;

  &.lg { width: 44px; height: 44px; border-radius: 11px; font-size: 16px; }
}

.proj-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.proj-name {
  font-size: 13.5px;
  font-weight: 500;
  color: var(--gray-900);
}

.proj-desc {
  font-size: 11.5px;
  color: var(--gray-500);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 320px;
}

.lang-chip {
  display: inline-flex;
  align-items: center;
  height: 22px;
  padding: 0 8px;
  border-radius: 4px;
  background: var(--gray-100);
  color: var(--gray-700);
  font-size: 11px;
}

.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 22px;
  padding: 0 10px;
  border-radius: 999px;
  font-size: 11.5px;
  font-weight: 500;

  .pill-dot { width: 6px; height: 6px; border-radius: 50%; }

  &.s-active {
    color: var(--status-fixed);
    background: rgba(79, 184, 122, 0.12);
    .pill-dot { background: var(--status-fixed); }
  }
  &.s-archived {
    color: var(--gray-500);
    background: var(--gray-100);
    .pill-dot { background: var(--gray-400); }
  }
}

.mini-gauge {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  position: relative;
  width: 44px;
  height: 36px;

  .gauge-svg { width: 36px; height: 36px; }
  .gauge-text {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 11px;
    font-weight: 600;
    width: 36px;
    height: 36px;
  }

  &.sm {
    width: 32px;
    height: 32px;
    .gauge-svg, .gauge-text { width: 32px; height: 32px; font-size: 10px; }
  }
}

.file-count { color: var(--gray-700); }
.muted { color: var(--color-text-placeholder); }
.muted-2 { color: var(--gray-500); font-size: 12px; }

/* ============ 卡片视图 ============ */
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 14px;
}

.proj-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 18px;
  background: #fff;
  border: 1px solid var(--gray-100);
  border-radius: 12px;
  cursor: pointer;
  transition: all 0.2s ease;
  box-shadow: var(--shadow-1);

  &:hover {
    border-color: var(--brand-200);
    box-shadow: var(--shadow-3);
    transform: translateY(-2px);
  }
}

.proj-card-head {
  display: flex;
  align-items: center;
  gap: 12px;
}

.head-meta {
  flex: 1;
  min-width: 0;
}

.card-name {
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 600;
  color: var(--gray-900);
  margin-bottom: 2px;
}

.card-sub {
  font-size: 11px;
  color: var(--gray-500);
}

.card-desc {
  font-size: 13px;
  color: var(--gray-600);
  line-height: 1.55;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  min-height: 40px;
}

.card-spectrum > div { height: 4px; }

.card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-top: 10px;
  border-top: 1px solid var(--gray-100);
}

.foot-meta {
  font-size: 11px;
  color: var(--gray-500);
}

.card-empty { grid-column: 1 / -1; }

/* ============ 分页 ============ */
.pagination-wrap {
  display: flex;
  justify-content: flex-end;
}
</style>
