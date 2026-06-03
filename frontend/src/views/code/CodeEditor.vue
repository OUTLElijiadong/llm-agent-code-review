<template>
  <div class="code-editor-page">
    <div class="page-header">
      <el-page-header @back="router.back()">
        <template #content>
          <span class="header-title">{{ fileDetail?.file_name || '代码编辑器' }}</span>
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
      <template v-if="fileDetail">
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
 * 代码编辑器页�?
 * 使用 MonacoEditor 组件进行代码查看与编辑
 */
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import MonacoEditor from '@/components/editor/MonacoEditor.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import { getDetail, update } from '@/api/codeFile'
import type { CodeFileDetailOut } from '@/types/project'

const route = useRoute()
const router = useRouter()

const fileId = Number(route.params.fileId)

const loading = ref(false)
const saving = ref(false)
const fileDetail = ref<CodeFileDetailOut | null>(null)
const codeContent = ref('')

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
 * 获取文件详情
 */
async function fetchDetail(): Promise<void> {
  loading.value = true
  try {
    const detail = await getDetail(fileId)
    fileDetail.value = detail
    codeContent.value = detail.content
  } catch {
    fileDetail.value = null
  } finally {
    loading.value = false
  }
}

/**
 * 保存文件
 */
async function handleSave(): Promise<void> {
  if (saving.value || !fileDetail.value) return
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
 * 键盘快捷键处�?
 */
function onKeydown(e: KeyboardEvent): void {
  if ((e.metaKey || e.ctrlKey) && e.key === 's') {
    e.preventDefault()
    handleSave()
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
</style>
