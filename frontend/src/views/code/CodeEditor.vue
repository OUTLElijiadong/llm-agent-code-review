<template>
  <div class="code-editor-page">
    <div class="page-header">
      <el-page-header @back="goBack(router)">
        <template #content>
          <span class="header-title">{{ fileDetail?.file_name || '代码编辑器' }}</span>
          <el-tag v-if="isBinary" size="small" type="warning" class="binary-tag">二进制</el-tag>
        </template>
        <template #extra>
          <div class="header-extra">
            <el-tag v-if="fileDetail" size="small" type="info">
              版本 v{{ fileDetail.version_no }}
            </el-tag>
          </div>
        </template>
      </el-page-header>
    </div>

    <div v-loading="loading" class="editor-content">
      <!-- v3: 二进制文件不展示 Monaco 编辑器,改为提示卡片 + 下载按钮 + 元信息 -->
      <template v-if="fileDetail && isBinary">
        <div class="binary-view">
          <el-icon class="binary-icon"><Document /></el-icon>
          <div class="binary-info">
            <h3>{{ fileDetail.file_name }}</h3>
            <p class="binary-hint">⚠️ 二进制文件,无法直接编辑。请下载后使用对应工具打开。</p>
            <el-descriptions :column="1" border size="small" class="binary-meta-desc">
              <el-descriptions-item label="文件名">{{ fileDetail.file_name }}</el-descriptions-item>
              <el-descriptions-item label="文件路径">
                {{ fileDetail.file_path || '-' }}
              </el-descriptions-item>
              <el-descriptions-item label="文件大小">
                {{ formatSize(fileDetail.size_bytes) }}
                <span v-if="fileDetail.raw_size && fileDetail.raw_size !== fileDetail.size_bytes" class="meta-aux">
                  (原始 {{ formatSize(fileDetail.raw_size) }})
                </span>
              </el-descriptions-item>
              <el-descriptions-item label="文件类型">
                {{ fileDetail.mime_type || fileTypeLabel }}
              </el-descriptions-item>
              <el-descriptions-item label="语言识别">
                {{ fileDetail.language || '-' }}
              </el-descriptions-item>
              <el-descriptions-item label="MD5">
                <code v-if="fileDetail.md5_hash" class="hash-value">{{ fileDetail.md5_hash }}</code>
                <span v-else class="text-muted">-</span>
              </el-descriptions-item>
              <el-descriptions-item label="SHA-256">
                <code v-if="fileDetail.sha256_hash" class="hash-value">{{ fileDetail.sha256_hash }}</code>
                <span v-else class="text-muted">-</span>
              </el-descriptions-item>
              <el-descriptions-item label="版本">v{{ fileDetail.version_no }}</el-descriptions-item>
              <el-descriptions-item label="更新时间">
                {{ formatDateTime(fileDetail.update_time) }}
              </el-descriptions-item>
            </el-descriptions>
            <div class="binary-actions">
              <el-button type="primary" :icon="Download" :loading="downloading" @click="handleDownload">
                下载文件
              </el-button>
            </div>
          </div>
        </div>
      </template>
      <!-- 文本文件:展示 Monaco 编辑器 -->
      <template v-else-if="fileDetail">
        <div class="editor-toolbar">
          <span class="toolbar-lang">{{ fileDetail.language }}</span>
          <el-button type="primary" size="small" :loading="saving" @click="handleSave">
            保存 (Ctrl+S)
          </el-button>
        </div>
        <MonacoEditor
          v-model="codeContent"
          :language="editorLang"
          :height="'calc(100vh - 180px)'"
        />
      </template>
      <EmptyState v-else-if="!loading" description="文件不存在或已删除" />
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 代码编辑器页面
 * 使用 MonacoEditor 组件进行代码查看与编辑
 * v3 增强:
 *  - 二进制文件(is_binary=1)不展示编辑器,改为提示卡片 + 元信息 + 下载按钮
 *  - 移除任何直接以 base64 字符串显示的逻辑
 *  - 支持展示 MD5/SHA-256/MIME 类型等元信息
 */
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { goBack } from '@/utils/navigation'

import { Document, Download } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import MonacoEditor from '@/components/editor/MonacoEditor.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import { getDetail, update, downloadBinary } from '@/api/codeFile'
import type { CodeFileDetailOut } from '@/types/project'
import { ElMessage } from 'element-plus/es/components/message/index'

const route = useRoute()
const router = useRouter()

const fileId = Number(route.params.fileId)

const loading = ref(false)
const saving = ref(false)
const downloading = ref(false)
const fileDetail = ref<CodeFileDetailOut | null>(null)
const codeContent = ref('')

/** 是否二进制文件(根据 is_binary 字段判断) */
const isBinary = computed(() => fileDetail.value?.is_binary === 1)

/**
 * 根据 MIME 类型或扩展名推断文件类型标签(用于元信息展示)
 * @returns 文件类型中文标签
 */
const fileTypeLabel = computed<string>(() => {
  const mime = fileDetail.value?.mime_type || ''
  const fileName = fileDetail.value?.file_name || ''
  const ext = fileName.split('.').pop()?.toLowerCase() || ''
  if (mime.startsWith('image/')) return '图片'
  if (mime.startsWith('video/')) return '视频'
  if (mime.startsWith('audio/')) return '音频'
  if (mime === 'application/zip' || mime === 'application/x-zip-compressed' || ext === 'zip') return '压缩包(ZIP)'
  if (mime === 'application/gzip' || mime === 'application/x-gzip' || ['gz', 'tgz', 'tar.gz'].includes(ext)) return '压缩包(GZIP)'
  if (mime === 'application/x-tar' || ext === 'tar') return '压缩包(TAR)'
  if (mime === 'application/pdf' || ext === 'pdf') return 'PDF 文档'
  if (mime === 'application/x-msdownload' || ['exe', 'dll', 'so', 'dylib'].includes(ext)) return '可执行文件'
  return '二进制文件'
})

/**
 * 将语言名称映射为 Monaco Editor 语言标识
 * @returns Monaco 语言标识
 */
const editorLang = computed(() => {
  const langMap: Record<string, string> = {
    python: 'python',
    javascript: 'javascript',
    typescript: 'typescript',
    java: 'java',
    go: 'go',
    cpp: 'cpp',
    c: 'c',
    vue: 'html',
    css: 'css',
    json: 'json',
    yaml: 'yaml',
    xml: 'xml',
    html: 'html',
  }
  return langMap[fileDetail.value?.language || ''] || 'text'
})

/**
 * 格式化文件大小为人类可读字符串
 * @param bytes 字节数
 * @returns 格式化后的字符串,如 "1.2 KB"
 */
function formatSize(bytes: number): string {
  if (!bytes || bytes < 0) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

/**
 * 格式化日期时间
 * @param dateStr ISO 日期字符串
 * @returns YYYY-MM-DD HH:mm 格式字符串
 */
function formatDateTime(dateStr: string): string {
  return dayjs(dateStr).format('YYYY-MM-DD HH:mm')
}

/**
 * 获取文件详情
 * v3: 二进制文件 content 由后端置空,前端不读取 base64,直接展示元信息卡片
 */
async function fetchDetail(): Promise<void> {
  loading.value = true
  try {
    const detail = await getDetail(fileId)
    fileDetail.value = detail
    // v3: 仅文本文件将 content 注入编辑器;二进制文件 content 已被后端置空,跳过
    if (detail.is_binary !== 1) {
      codeContent.value = detail.content
    } else {
      codeContent.value = ''
    }
  } catch {
    fileDetail.value = null
  } finally {
    loading.value = false
  }
}

/**
 * 保存文件(仅文本文件可调用)
 */
async function handleSave(): Promise<void> {
  if (saving.value || !fileDetail.value || isBinary.value) return
  saving.value = true
  try {
    const result = await update(fileId, { content: codeContent.value })
    fileDetail.value.version_no = result.version_no
    ElMessage.success('保存成功')
  } catch {
    // 错误已在拦截器处理
  } finally {
    saving.value = false
  }
}

/**
 * 下载二进制文件(触发浏览器下载)
 */
async function handleDownload(): Promise<void> {
  if (downloading.value || !fileDetail.value) return
  downloading.value = true
  try {
    const blob = await downloadBinary(fileId)
    // 触发浏览器下载
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = fileDetail.value.file_name
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  } catch {
    // 错误已在拦截器处理
  } finally {
    downloading.value = false
  }
}

/**
 * 键盘快捷键处理(仅文本文件响应 Ctrl+S)
 */
function onKeydown(e: KeyboardEvent): void {
  if ((e.metaKey || e.ctrlKey) && e.key === 's') {
    e.preventDefault()
    if (!isBinary.value) handleSave()
  }
}

onMounted(() => {
  fetchDetail()
  document.addEventListener('keydown', onKeydown)
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown)
})
</script>

<style scoped lang="scss">
.code-editor-page {
  height: calc(100vh - 60px);
  display: flex;
  flex-direction: column;
}

.page-header {
  padding: 12px 24px;
  border-bottom: 1px solid var(--el-border-color-light);
  flex-shrink: 0;
}

.header-title {
  font-size: 16px;
  font-weight: 600;
}

.binary-tag {
  margin-left: 8px;
}

.header-extra {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-left: 16px;
}

.editor-content {
  flex: 1;
  overflow: hidden;
}

.editor-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  background: var(--el-fill-color-light);
  border-bottom: 1px solid var(--el-border-color-light);
}

.toolbar-lang {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

/* v3: 二进制文件下载视图样式 */
.binary-view {
  display: flex;
  align-items: flex-start;
  justify-content: center;
  height: 100%;
  padding: 40px;
  overflow: auto;
}

.binary-icon {
  font-size: 80px;
  color: var(--el-color-warning);
  margin-right: 32px;
  flex-shrink: 0;
}

.binary-info {
  max-width: 560px;
  width: 100%;

  h3 {
    margin: 0 0 12px;
    font-size: 20px;
    color: var(--el-text-color-primary);
    word-break: break-all;
  }

  .binary-meta-desc {
    margin: 12px 0 16px;
  }

  .meta-aux {
    color: var(--el-text-color-secondary);
    font-size: 12px;
    margin-left: 4px;
  }

  .hash-value {
    font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
    font-size: 12px;
    color: var(--el-text-color-regular);
    word-break: break-all;
  }

  .text-muted {
    color: var(--el-text-color-placeholder);
  }

  .binary-hint {
    margin: 0 0 16px;
    font-size: 13px;
    color: var(--el-color-warning);
    line-height: 1.6;
  }

  .binary-actions {
    display: flex;
    gap: 12px;
  }
}
</style>
