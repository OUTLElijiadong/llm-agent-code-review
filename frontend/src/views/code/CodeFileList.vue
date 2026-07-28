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
      <el-table-column prop="file_name" label="文件名" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">
          <el-icon class="file-icon" :class="`file-icon-${fileCategory(row).key}`">
            <component :is="fileCategory(row).icon" />
          </el-icon>
          <span class="file-name-text">{{ row.file_name }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="language" label="语言" width="120" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.language" size="small" type="info">{{ row.language }}</el-tag>
          <span v-else class="text-muted">-</span>
        </template>
      </el-table-column>
      <el-table-column label="类型" width="110" align="center">
        <template #default="{ row }">
          <el-tag
            size="small"
            :type="fileCategory(row).tagType"
            effect="light"
          >
            {{ fileCategory(row).label }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="size_bytes" label="大小" width="110" align="center">
        <template #default="{ row }">
          <span :class="{ 'text-muted': row.size_bytes === 0 }">{{ formatFileSize(row.size_bytes) }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="line_count" label="行数" width="100" align="center">
        <template #default="{ row }">
          <span v-if="row.is_binary === 1" class="text-muted">-</span>
          <span v-else>{{ row.line_count }}</span>
        </template>
      </el-table-column>
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
      <el-table-column label="操作" width="220" align="center" fixed="right">
        <template #default="{ row }">
          <template v-if="row.is_binary === 1">
            <el-button link type="primary" :loading="downloadingId === row.id" @click="handleDownload(row)">
              <el-icon><Download /></el-icon>下载
            </el-button>
            <el-button link type="info" @click="handleView(row)">查看元信息</el-button>
          </template>
          <template v-else>
            <el-button link type="primary" @click="handleView(row)">查看代码</el-button>
            <el-button link type="primary" @click="handleHistory(row)">版本历史</el-button>
          </template>
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
 * 展示项目下的代码文件,支持语言筛选、查看代码和版本历史
 * v3 增强:
 *  - 文件列表中二进制文件显示专用图标(Picture/Archive/Document)
 *  - 二进制文件不能点击编辑,改为下载或查看元信息
 *  - 显示文件大小(使用 formatFileSize 函数格式化)
 *  - 添加文件类型徽章(文本/图片/压缩包/二进制)
 *  - 修复:确保压缩包上传后内部文件正常显示,不显示 base64 内容
 */
import { ref, onMounted, type Component } from 'vue'
import { useRouter } from 'vue-router'

import { Picture, Files, Document, Download } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import { list, downloadBinary } from '@/api/codeFile'
import type { CodeFileOut } from '@/types/project'
import { ElMessage } from 'element-plus/es/components/message/index'

/** 文件分类信息 */
interface FileCategory {
  /** 分类 key,用于 CSS class */
  key: 'text' | 'image' | 'archive' | 'binary'
  /** 中文标签 */
  label: string
  /** Element Plus 图标组件 */
  icon: Component
  /** Element Plus Tag type */
  tagType: 'info' | 'success' | 'warning' | 'danger'
}

const props = defineProps<{
  projectId: number
}>()

const router = useRouter()

const loading = ref(false)
const files = ref<CodeFileOut[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(10)
const languageFilter = ref('')
/** v2: 当前正在下载的二进制文件 ID,用于按钮 loading 状态 */
const downloadingId = ref<number | null>(null)

/**
 * 格式化日期
 * @param dateStr - 日期字符串
 * @returns 格式化后的日期字符串
 */
function formatDate(dateStr: string): string {
  return dayjs(dateStr).format('YYYY-MM-DD HH:mm')
}

/**
 * 格式化文件大小为人类可读字符串
 * v3:对齐 T13 规范命名(formatFileSize)
 * @param bytes - 文件字节数
 * @returns 可读的文件大小字符串
 */
function formatFileSize(bytes: number): string {
  if (!bytes || bytes < 0) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

/**
 * 根据文件信息推断文件分类(文本/图片/压缩包/二进制)
 * v3 新增:用于在列表中显示专用图标和类型徽章
 * @param row - 文件项
 * @returns 文件分类信息
 */
function fileCategory(row: CodeFileOut): FileCategory {
  // 二进制文件:进一步区分图片/压缩包/其他二进制
  if (row.is_binary === 1) {
    const mime = row.mime_type || ''
    const ext = row.file_name.split('.').pop()?.toLowerCase() || ''
    // 图片
    if (mime.startsWith('image/') || ['png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp', 'svg', 'ico'].includes(ext)) {
      return { key: 'image', label: '图片', icon: Picture, tagType: 'success' }
    }
    // 压缩包
    if (
      mime === 'application/zip' ||
      mime === 'application/x-zip-compressed' ||
      mime === 'application/gzip' ||
      mime === 'application/x-gzip' ||
      mime === 'application/x-tar' ||
      mime === 'application/x-rar-compressed' ||
      ['zip', 'gz', 'tgz', 'tar', 'rar', '7z', 'bz2'].includes(ext)
    ) {
      return { key: 'archive', label: '压缩包', icon: Files, tagType: 'warning' }
    }
    // 其他二进制
    return { key: 'binary', label: '二进制', icon: Document, tagType: 'danger' }
  }
  // 文本文件
  return { key: 'text', label: '文本', icon: Document, tagType: 'info' }
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
 * 查看代码/查看元信息
 * v3:二进制文件同样跳转到 CodeEditor 页,由 CodeEditor 自行渲染元信息卡片
 * @param row - 文件项
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

/**
 * 下载二进制文件(触发浏览器下载)
 * v2 新增:二进制文件不进入编辑器,直接下载原文件
 * v3:同时确保不会把 base64 内容塞入编辑器
 * @param row - 文件项
 */
async function handleDownload(row: CodeFileOut): Promise<void> {
  if (downloadingId.value !== null) return
  downloadingId.value = row.id
  try {
    const blob = await downloadBinary(row.id)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = row.file_name
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    ElMessage.success('文件下载已开始')
  } catch {
    /* 错误已在拦截器处理 */
  } finally {
    downloadingId.value = null
  }
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

  /* v3: 文件名单元格图标 + 文本对齐 */
  .file-icon {
    margin-right: 6px;
    vertical-align: middle;
    font-size: 16px;

    &.file-icon-text {
      color: var(--el-color-info);
    }

    &.file-icon-image {
      color: var(--el-color-success);
    }

    &.file-icon-archive {
      color: var(--el-color-warning);
    }

    &.file-icon-binary {
      color: var(--el-color-danger);
    }
  }

  .file-name-text {
    vertical-align: middle;
  }
}
</style>
