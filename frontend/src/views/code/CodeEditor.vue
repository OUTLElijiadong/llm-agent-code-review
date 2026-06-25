<template>
  <div class="code-editor-page">
    <div class="page-header">
      <el-page-header @back="router.back()">
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
      <!-- v2: 二进制文件不展示 Monaco 编辑器,改为下载视图 -->
      <template v-if="fileDetail && isBinary">
        <div class="binary-view">
          <el-icon class="binary-icon"><Document /></el-icon>
          <div class="binary-info">
            <h3>{{ fileDetail.file_name }}</h3>
            <p class="binary-meta">
              类型:{{ fileDetail.language }} · 大小:{{ formatSize(fileDetail.size_bytes) }}
            </p>
            <p class="binary-hint">该文件为二进制文件,无法在编辑器中查看,请下载后使用对应工具打开。</p>
            <el-button type="primary" :icon="Download" :loading="downloading" @click="handleDownload">
              下载文件
            </el-button>
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
 * v2 增强:二进制文件(is_binary=1)不展示编辑器,改为下载视图
 */
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Document, Download } from '@element-plus/icons-vue'
import MonacoEditor from '@/components/editor/MonacoEditor.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import { getDetail, update, downloadBinary } from '@/api/codeFile'
import type { CodeFileDetailOut } from '@/types/project'

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
 * 将语言名称映射为 Monaco Editor 语言标识
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
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

/**
 * 获取文件详情
 */
async function fetchDetail(): Promise<void> {
  loading.value = true
  try {
    const detail = await getDetail(fileId)
    fileDetail.value = detail
    // v2: 二进制文件 content 已被后端置空,无需塞入编辑器
    if (detail.is_binary !== 1) {
      codeContent.value = detail.content
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

/* v2: 二进制文件下载视图样式 */
.binary-view {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  padding: 40px;
}

.binary-icon {
  font-size: 80px;
  color: var(--el-color-warning);
  margin-right: 32px;
}

.binary-info {
  max-width: 400px;

  h3 {
    margin: 0 0 12px;
    font-size: 20px;
    color: var(--el-text-color-primary);
  }

  .binary-meta {
    margin: 0 0 8px;
    font-size: 14px;
    color: var(--el-text-color-secondary);
  }

  .binary-hint {
    margin: 0 0 20px;
    font-size: 13px;
    color: var(--el-text-color-secondary);
    line-height: 1.6;
  }
}
</style>
