<template>
  <!-- v3: 二进制文件不展示 Monaco 编辑器,改为提示卡片 -->
  <div v-if="isBinary" class="binary-viewer">
    <el-icon class="binary-icon"><Document /></el-icon>
    <div class="binary-info">
      <p class="binary-hint">⚠️ 二进制文件,无法直接查看。请在「代码」页面下载原文件后使用对应工具打开。</p>
      <el-descriptions v-if="binaryMeta" :column="1" border size="small">
        <el-descriptions-item label="文件名">{{ binaryMeta.file_name }}</el-descriptions-item>
        <el-descriptions-item label="大小">{{ formatFileSize(binaryMeta.size_bytes) }}</el-descriptions-item>
        <el-descriptions-item label="类型">{{ binaryMeta.mime_type || '二进制文件' }}</el-descriptions-item>
      </el-descriptions>
    </div>
  </div>
  <MonacoEditor
    v-else
    :model-value="code"
    :language="language"
    :readonly="true"
    :height="height"
    :highlight-lines="highlightLines"
    ref="editorRef"
  />
</template>

<script setup lang="ts">
/**
 * 代码查看器组件(只读)
 * v3 增强:支持二进制文件标识,二进制文件不展示 Monaco 编辑器,改为提示卡片
 * 用于 ReviewTaskDetail 等只读场景
 */
import { ref } from 'vue'
import { Document } from '@element-plus/icons-vue'
import MonacoEditor from '@/components/editor/MonacoEditor.vue'
import type { CodeFileMetaOut } from '@/types/project'

const props = defineProps<{
  /** 文本内容(二进制文件应传入空字符串) */
  code: string
  /** Monaco 语言标识 */
  language?: string
  /** 容器高度 */
  height?: string
  /** 需要高亮的行号列表 */
  highlightLines?: number[]
  /** v3: 是否二进制文件 */
  isBinary?: boolean
  /** v3: 二进制文件元信息(仅 isBinary 为 true 时使用) */
  binaryMeta?: CodeFileMetaOut | null
}>()

const editorRef = ref<InstanceType<typeof MonacoEditor> | null>(null)

/**
 * 格式化文件大小为人类可读字符串
 * @param bytes 字节数
 * @returns 格式化后的字符串
 */
function formatFileSize(bytes: number): string {
  if (!bytes || bytes < 0) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

/**
 * 滚动到指定行(仅文本文件可用)
 * @param lineNumber 行号
 */
function revealLine(lineNumber: number): void {
  if (props.isBinary) return
  editorRef.value?.revealLine(lineNumber)
}

/** 代码字号(持久化,两键缩放的范围限制)。 */
const FONT_KEY = 'prism-code-font-size'
const fontSize = ref(Number(localStorage.getItem(FONT_KEY)) || 13)

function applyFontSize(size: number): void {
  fontSize.value = size
  localStorage.setItem(FONT_KEY, String(size))
  editorRef.value?.updateOptions({ fontSize: size })
}

/** 放大/缩小代码字号(工具栏 ZoomIn/ZoomOut 接入)。 */
function zoomIn(): void {
  applyFontSize(Math.min(22, fontSize.value + 1))
}

function zoomOut(): void {
  applyFontSize(Math.max(10, fontSize.value - 1))
}

defineExpose({ revealLine, zoomIn, zoomOut })
</script>

<style scoped lang="scss">
.binary-viewer {
  display: flex;
  align-items: flex-start;
  justify-content: center;
  height: 100%;
  padding: 32px;
  overflow: auto;

  .binary-icon {
    font-size: 56px;
    color: var(--el-color-warning);
    margin-right: 20px;
    flex-shrink: 0;
  }

  .binary-info {
    max-width: 480px;

    .binary-hint {
      margin: 0 0 12px;
      font-size: 13px;
      color: var(--el-color-warning);
      line-height: 1.6;
    }
  }
}
</style>
