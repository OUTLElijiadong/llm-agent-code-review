<template>
  <div class="issue-hub-page">
    <div class="page-header">
      <div>
        <h2>问题追踪中心</h2>
        <p class="page-sub">跨项目统一查看所有审查问题，支持筛选与状态流转</p>
      </div>
      <el-button type="primary" @click="goReviewList">查看审查任务</el-button>
    </div>

    <el-card shadow="hover" class="filter-card">
      <div class="filter-bar">
        <el-select
          v-model="filters.project_id"
          placeholder="项目"
          clearable
          style="width: 180px"
          @change="reload"
        >
          <el-option
            v-for="p in projects"
            :key="p.id"
            :label="p.project_name"
            :value="p.id"
          />
        </el-select>
        <el-select v-model="filters.severity" placeholder="严重程度" clearable style="width: 140px" @change="reload">
          <el-option label="严重" value="严重" />
          <el-option label="高" value="高" />
          <el-option label="中" value="中" />
          <el-option label="低" value="低" />
        </el-select>
        <el-select v-model="filters.issue_type" placeholder="问题类型" clearable style="width: 160px" @change="reload">
          <el-option label="代码规范" value="代码规范" />
          <el-option label="潜在Bug" value="潜在Bug" />
          <el-option label="安全漏洞" value="安全漏洞" />
          <el-option label="性能问题" value="性能问题" />
          <el-option label="异常处理" value="异常处理" />
          <el-option label="命名规范" value="命名规范" />
          <el-option label="可维护性" value="可维护性" />
          <el-option label="注释完整性" value="注释完整性" />
        </el-select>
        <el-select v-model="filters.status" placeholder="状态" clearable style="width: 140px" @change="reload">
          <el-option label="全部状态" value="all" />
          <el-option label="未修复" value="unfixed" />
          <el-option label="已修复" value="fixed" />
          <el-option label="已忽略" value="ignored" />
          <el-option label="待复查" value="pending_review" />
        </el-select>
        <el-input
          v-model="filters.keyword"
          placeholder="搜索标题或描述"
          clearable
          style="width: 220px"
          @input="reloadDebounced"
          @change="reload"
        />
        <el-button type="primary" :disabled="!selected.length" @click="onBatchMarkFixed">
          批量标记已修复 ({{ selected.length }})
        </el-button>
      </div>
    </el-card>

    <el-card shadow="hover">
      <el-table
        v-loading="loading"
        :data="rows"
        stripe
        empty-text="暂无问题"
        @selection-change="onSelectionChange"
      >
        <el-table-column type="selection" width="50" />
        <el-table-column prop="project_name" label="项目" width="160" show-overflow-tooltip />
        <el-table-column prop="file_name" label="文件" width="180" show-overflow-tooltip />
        <el-table-column prop="line_number" label="行号" width="80" align="center" />
        <el-table-column prop="severity" label="严重度" width="100" align="center">
          <template #default="{ row }">
            <el-tag :type="severityTag(row.severity)" size="small">{{ severityLabel(row.severity) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="issue_type" label="类型" width="120" align="center">
          <template #default="{ row }">
            <el-tag size="small" type="info" effect="plain">{{ typeLabel(row.issue_type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="title" label="问题" min-width="220" show-overflow-tooltip />
        <el-table-column prop="status" label="状态" width="110" align="center">
          <template #default="{ row }">
            <el-tag :type="statusTag(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220" align="center" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="onJump(row)">查看任务</el-button>
            <el-dropdown trigger="click" @command="(s: string) => onSetStatus(row, s)">
              <el-button link type="primary">改状态<el-icon><ArrowDown /></el-icon></el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="fixed">标记已修复</el-dropdown-item>
                  <el-dropdown-item command="ignored">标记已忽略</el-dropdown-item>
                  <el-dropdown-item command="pending_review">标记待复查</el-dropdown-item>
                  <el-dropdown-item command="unfixed">恢复未修复</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination-wrapper">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          @change="loadIssues"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { ArrowDown } from '@element-plus/icons-vue'
import { list as listIssues, updateStatus, batchUpdateStatus } from '@/api/issue'
import { getProjects } from '@/api/project'
import type { IssueListItemOut } from '@/types/review'
import type { ProjectOut } from '@/types/project'
import { severityClass, severityDisplayLabel } from '@/constants/severity'
import { dimLabel } from '@/constants/dim'
import { ElMessage } from 'element-plus/es/components/message/index'
import { confirmDanger } from '@/composables/useDangerConfirm'

const router = useRouter()

const loading = ref(false)
const rows = ref<IssueListItemOut[]>([])
const selected = ref<IssueListItemOut[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const projects = ref<ProjectOut[]>([])

const filters = reactive({
  project_id: undefined as number | undefined,
  severity: '',
  issue_type: '',
  status: '',
  keyword: '',
})

function severityTag(s: string): 'danger' | 'warning' | 'primary' | 'info' {
  // v2.0: 兼容中英文混存 (severityClass 把 high/medium/severe 归一化为 key)
  const key = severityClass(s)
  if (key === 'severe') return 'danger'
  if (key === 'high') return 'warning'
  if (key === 'medium') return 'primary'
  return 'info'
}

function severityLabel(s: string): string {
  return severityDisplayLabel(s)
}

function typeLabel(s: string): string {
  return dimLabel(s)
}

function statusTag(s: string): 'success' | 'info' | 'warning' | 'primary' {
  switch (s) {
    case 'fixed':
      return 'success'
    case 'ignored':
      return 'info'
    case 'pending_review':
      return 'warning'
    default:
      return 'primary'
  }
}

function statusLabel(s: string): string {
  return { unfixed: '未修复', fixed: '已修复', ignored: '已忽略', pending_review: '待复查' }[s] || s
}

async function loadProjects(): Promise<void> {
  const data = await getProjects({ page: 1, page_size: 100 })
  projects.value = data.items
}

async function loadIssues(): Promise<void> {
  loading.value = true
  try {
    const data = await listIssues({
      project_id: filters.project_id,
      severity: filters.severity || undefined,
      issue_type: filters.issue_type || undefined,
      status: filters.status || undefined,
      keyword: filters.keyword || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
    rows.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

function reload(): void {
  page.value = 1
  loadIssues()
}

// 关键词搜索防抖:输入停顿 400ms 后才触发,避免每次击键都请求
let searchTimer: ReturnType<typeof setTimeout> | null = null
function reloadDebounced(): void {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    reload()
  }, 400)
}

function onSelectionChange(rows: IssueListItemOut[]): void {
  selected.value = rows
}

async function onSetStatus(row: IssueListItemOut, status: string): Promise<void> {
  try {
    await updateStatus(row.id, { status })
    ElMessage.success('状态已更新')
    if (status === 'fixed' || status === 'ignored') {
      loadIssues()
    } else {
      row.status = status
    }
  } catch {
    ElMessage.error('状态更新失败')
  }
}

async function onBatchMarkFixed(): Promise<void> {
  if (!selected.value.length) return
  // 统一危险确认,避免误点一次批量改几十条状态
  const ok = await confirmDanger({
    target: `将选中的 ${selected.value.length} 条问题标记为已修复`,
    consequence: '标记后需逐条核实才能改回',
    confirmText: '确定标记',
  })
  if (!ok) return
  try {
    await batchUpdateStatus({
      ids: selected.value.map((r) => r.id),
      status: 'fixed',
    })
    ElMessage.success(`已批量标记 ${selected.value.length} 条为已修复`)
    selected.value = []
    loadIssues()
  } catch {
    ElMessage.error('批量更新失败')
  }
}

function onJump(row: IssueListItemOut): void {
  router.push(`/reviews/${row.task_id}`)
}

function goReviewList(): void {
  router.push('/reviews')
}

onMounted(async () => {
  await loadProjects()
  await loadIssues()
})
</script>

<style scoped lang="scss">
.issue-hub-page {
  padding: var(--spacing-lg);
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  margin-bottom: var(--spacing-lg);

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

.filter-card {
  margin-bottom: var(--spacing-md);
}

.filter-bar {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  align-items: center;
}

.pagination-wrapper {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
</style>
