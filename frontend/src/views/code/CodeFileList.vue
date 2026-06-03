<template>
  <div class="code-file-list">
    <div class="toolbar">
      <el-select
        v-model="languageFilter"
        placeholder="语言筛选"
        clearable
        class="filter-select"
        @change="handleFilterChange"
      >
        <el-option label="Python" value="python" />
        <el-option label="JavaScript" value="javascript" />
        <el-option label="TypeScript" value="typescript" />
        <el-option label="Java" value="java" />
        <el-option label="Go" value="go" />
        <el-option label="C++" value="cpp" />
        <el-option label="Vue" value="vue" />
        <el-option label="CSS" value="css" />
        <el-option label="JSON" value="json" />
      </el-select>
    </div>

    <el-table
      v-loading="loading"
      :data="files"
      border
      stripe
      empty-text="暂无代码文件"
    >
      <el-table-column prop="file_name" label="文件名" min-width="200" show-overflow-tooltip />
      <el-table-column prop="language" label="语言" width="120" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.language" size="small" type="info">{{ row.language }}</el-tag>
          <span v-else class="text-muted">-</span>
        </template>
      </el-table-column>
      <el-table-column prop="size_bytes" label="大小" width="100" align="center">
        <template #default="{ row }">
          {{ formatSize(row.size_bytes) }}
        </template>
      </el-table-column>
      <el-table-column prop="line_count" label="行数" width="100" align="center" />
      <el-table-column prop="version_no" label="版本" width="80" align="center">
        <template #default="{ row }">
          v{{ row.version_no }}
        </template>
      </el-table-column>
      <el-table-column prop="update_time" label="更新时间" width="180" align="center">
        <template #default="{ row }">
          {{ formatDate(row.update_time) }}
        </template>
      </el-table-column>
      <el-table-column label="操作" width="180" align="center" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="handleView(row)">查看代码</el-button>
          <el-button link type="primary" @click="handleHistory(row)">版本历史</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div v-if="total > 0" class="pagination-wrap">
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="total"
        :page-sizes="[10, 20, 50]"
        layout="total, sizes, prev, pager, next"
        @size-change="fetchFiles"
        @current-change="fetchFiles"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 代码文件列表组件
 * 展示项目下的代码文件，支持语言筛选、查看代码和版本历史
 */
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import dayjs from 'dayjs'
import { list } from '@/api/codeFile'
import type { CodeFileOut } from '@/types/project'

const props = defineProps<{
  projectId: number
}>()

const emit = defineEmits<{
  uploaded: []
}>()

const router = useRouter()

const loading = ref(false)
const files = ref<CodeFileOut[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(10)
const languageFilter = ref('')

/**
 * 格式化日期
 * @param dateStr - 日期字符串
 * @returns 格式化后的日期字符�?
 */
function formatDate(dateStr: string): string {
  return dayjs(dateStr).format('YYYY-MM-DD HH:mm')
}

/**
 * 格式化文件大小
 * @param bytes - 文件字节数
 * @returns 可读的文件大小字符�?
 */
function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/**
 * 获取文件列表
 */
async function fetchFiles(): Promise<void> {
  loading.value = true
  try {
    const params: Record<string, unknown> = {
      project_id: props.projectId,
      page: page.value,
      page_size: pageSize.value,
    }
    if (languageFilter.value) params.language = languageFilter.value

    const res = await list(params)
    files.value = res.items
    total.value = res.total
  } catch {
    files.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

/**
 * 语言筛选变更
 */
function handleFilterChange(): void {
  page.value = 1
  fetchFiles()
}

/**
 * 查看代码
 * @param row - 文件�?
 */
function handleView(row: CodeFileOut): void {
  router.push(`/code/${props.projectId}/file/${row.id}`)
}

/**
 * 查看版本历史
 * @param row - 文件项
 */
function handleHistory(row: CodeFileOut): void {
  router.push(`/code/${props.projectId}/file/${row.id}/versions`)
}

onMounted(() => {
  fetchFiles()
})
</script>

<style scoped lang="scss">
.code-file-list {
  .toolbar {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 12px;
  }

  .filter-select {
    width: 140px;
  }

  .pagination-wrap {
    display: flex;
    justify-content: flex-end;
    margin-top: 16px;
  }

  .text-muted {
    color: var(--el-text-color-placeholder);
  }
}
</style>
