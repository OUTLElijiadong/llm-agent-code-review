<template>
  <el-dialog
    :model-value="visible"
    :title="mode === 'create' ? '新建项目' : '编辑项目'"
    :close-on-click-modal="false"
    width="580px"
    @update:model-value="$emit('update:visible', $event)"
    @close="handleClose"
    @open="handleOpen"
  >
    <el-form
      ref="formRef"
      :model="form"
      :rules="rules"
      label-width="90px"
      @submit.prevent="handleSubmit"
    >
      <el-form-item v-if="mode === 'create'" label="代码文件夹" prop="files">
        <div class="folder-upload-area">
          <input
            ref="folderInputRef"
            type="file"
            webkitdirectory
            directory
            multiple
            :disabled="analyzing"
            style="display: none"
            @change="onFolderSelected"
          />
          <div
            class="folder-drop-zone"
            :class="{ 'has-files': selectedFiles.length > 0, analyzing }"
            @click="handleSelectFolder"
            @dragover.prevent
            @drop.prevent="onDrop"
          >
            <template v-if="analyzing">
              <el-icon class="folder-icon is-spinning"><Loading /></el-icon>
              <p class="folder-text">Agent 正在分析文件夹...</p>
              <p class="folder-hint">正在识别项目名称、描述和编程语言</p>
            </template>
            <template v-else-if="selectedFiles.length === 0">
              <el-icon class="folder-icon"><FolderOpened /></el-icon>
              <p class="folder-text">点击或拖拽文件夹到此处</p>
              <p class="folder-hint">Agent 将自动识别项目名称、描述和编程语言</p>
            </template>
            <template v-else>
              <el-icon class="folder-icon filled"><FolderOpened /></el-icon>
              <p class="folder-text">已选择 <b>{{ selectedFiles.length }}</b> 个文件</p>
              <div class="file-path-box">
                <span class="file-path-label font-mono">{{ folderDisplayPath }}</span>
              </div>
            </template>
          </div>
        </div>
      </el-form-item>

      <template v-if="mode === 'create' && aiFilled">
        <el-alert
          type="success"
          :closable="false"
          show-icon
          class="ai-banner"
        >
          <template #title>
            Agent 已分析 {{ selectedFiles.length }} 个文件，自动填写以下信息
          </template>
        </el-alert>
      </template>

      <el-form-item label="项目名称" prop="project_name">
        <el-input
          v-model="form.project_name"
          :placeholder="mode === 'create' ? '可手动输入，选择文件夹后可由 Agent 自动生成' : '请输入项目名称'"
          maxlength="50"
          show-word-limit
        >
          <template v-if="aiFilled" #prefix>
            <el-tag type="success" size="small" effect="plain" class="field-tag">Agent</el-tag>
          </template>
        </el-input>
      </el-form-item>

      <el-form-item label="项目描述" prop="description">
        <el-input
          v-model="form.description"
          type="textarea"
          :rows="2"
          :placeholder="mode === 'create' ? '可手动输入，选择文件夹后可由 Agent 自动生成' : '请输入项目描述'"
          maxlength="200"
          show-word-limit
        >
          <template v-if="aiFilled && form.description" #prefix>
            <el-tag type="success" size="small" effect="plain" class="field-tag">Agent</el-tag>
          </template>
        </el-input>
      </el-form-item>

      <el-form-item label="编程语言" prop="language">
        <div class="lang-row">
          <el-tag
            v-if="form.language"
            type="primary"
            :color="langColor"
            effect="dark"
            size="large"
            class="lang-badge"
          >
            {{ languageName }}
          </el-tag>
          <span v-else class="text-muted">{{ mode === 'create' ? '未选择语言，可先手动创建' : '—' }}</span>
        </div>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="handleClose">取消</el-button>
      <el-button type="primary" :loading="submitting" :disabled="!canSubmit" @click="handleSubmit">
        {{ submitButtonText }}
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, reactive, computed } from 'vue'
import type { FormInstance, FormRules } from 'element-plus'
import { Loading, FolderOpened } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { analyzeFolder } from '@/api/project'
import type { ProjectOut } from '@/types/project'

interface Props {
  visible: boolean
  mode: 'create' | 'edit'
  initialData: ProjectOut | null
}

const props = defineProps<Props>()

const emit = defineEmits<{
  'update:visible': [value: boolean]
  submit: [data: {
    project_name: string
    description?: string
    language?: string
    files?: File[]
  }]
}>()

const formRef = ref<FormInstance>()
const folderInputRef = ref<HTMLInputElement>()
const submitting = ref(false)
const analyzing = ref(false)
const aiFilled = ref(false)
const selectedFiles = ref<File[]>([])
const folderDisplayPath = ref('')

const form = reactive({
  project_name: '',
  description: '',
  language: '',
})

const rules: FormRules = {
  project_name: [
    { required: true, message: '项目名称不能为空', trigger: 'blur' },
    { min: 1, max: 50, message: '长度在 1 到 50 个字符', trigger: 'blur' },
  ],
}

const canSubmit = computed(() => {
  if (props.mode === 'edit') return true
  return form.project_name.trim().length > 0
})

const submitButtonText = computed(() => {
  if (props.mode === 'edit') return '保存'
  return selectedFiles.value.length > 0 ? '创建项目并上传文件' : '创建项目'
})

const langPalette: Record<string, string> = {
  python: '#4B9BFF', javascript: '#D4A53A', typescript: '#4B9BFF',
  java: '#E27C4A', go: '#2BBFB9', cpp: '#B85AC4',
  vue: '#42B883', html: '#E27C4A', css: '#4B9BFF',
  php: '#787CB5', c: '#5B58E8', sql: '#E08648', plaintext: '#6E7689',
}

const langNames: Record<string, string> = {
  python: 'Python', javascript: 'JavaScript', typescript: 'TypeScript',
  java: 'Java', go: 'Go', cpp: 'C++', vue: 'Vue',
  html: 'HTML', css: 'CSS', php: 'PHP', c: 'C', sql: 'SQL', plaintext: '未知',
}

const langColor = computed(() => langPalette[form.language] || '#6E7689')
const languageName = computed(() => langNames[form.language] || form.language || '—')

/**
 * 打开弹窗时初始化表单、文件列表和 AI 分析状态
 * @returns void
 */
function handleOpen(): void {
  analyzing.value = false
  aiFilled.value = false
  selectedFiles.value = []
  folderDisplayPath.value = ''
  if (props.mode === 'edit' && props.initialData) {
    form.project_name = props.initialData.project_name
    form.description = props.initialData.description || ''
    form.language = props.initialData.language || ''
  } else {
    form.project_name = ''
    form.description = ''
    form.language = ''
  }
}

/**
 * 关闭项目表单弹窗
 * @returns void
 */
function handleClose(): void {
  emit('update:visible', false)
}

/**
 * 触发文件夹选择器
 * @returns void
 */
function handleSelectFolder(): void {
  if (analyzing.value) return
  folderInputRef.value?.click()
}

const SYSTEM_DIRS = /(^|\/)(\.git|\.svn|\.hg|node_modules|__pycache__|\.next|dist|build|target|\.idea|\.vscode)(\/|$)/

/**
 * 收集文件夹中所有文件(过滤系统目录,完整上传)
 * @param fileList - 浏览器文件夹选择返回的文件列表
 * @returns 所有文件、相对路径和文件夹名
 */
function extractFiles(fileList: FileList): { files: File[]; names: string[]; folderName: string } {
  const allFiles: File[] = []
  const allNames: string[] = []
  let folderName = ''
  let skipped = 0

  for (let i = 0; i < fileList.length; i++) {
    const f = fileList[i]
    if (!folderName && f.webkitRelativePath) {
      folderName = f.webkitRelativePath.split('/')[0] || ''
    }
    const relativePath = f.webkitRelativePath || f.name
    if (SYSTEM_DIRS.test(relativePath)) {
      skipped++
      continue
    }
    allFiles.push(f)
    allNames.push(relativePath)
  }

  return { files: allFiles, names: allNames, folderName }
}

/**
 * 保存已选文件并触发 Agent 文件夹分析
 * @param files - 所有文件
 * @param names - 文件相对路径列表
 * @param folderName - 文件夹名称
 * @param totalCount - 用户选择的原始文件数量
 * @returns Promise<void>
 */
async function processFolder(files: File[], names: string[], folderName: string, totalCount: number): Promise<void> {
  selectedFiles.value = files
  folderDisplayPath.value = folderName || '已选择文件夹'
  if (files.length === 0) {
    ElMessage.warning(`未检测到文件（共 ${totalCount} 个文件，已过滤系统目录）`)
    return
  }
  const skipped = totalCount - files.length
  const hint = skipped > 0 ? `（已跳过 ${skipped} 个系统目录文件）` : ''
  ElMessage.info(`已选择 ${files.length} 个文件${hint}，Agent 分析中...`)
  await autoAnalyzeFolder(folderName, names)
}

/**
 * 处理文件夹选择器返回的文件
 * @param e - 文件输入事件
 * @returns void
 */
function onFolderSelected(e: Event): void {
  const input = e.target as HTMLInputElement
  const fileList = input.files
  if (!fileList || fileList.length === 0) return
  const totalCount = fileList.length
  const { files, names, folderName } = extractFiles(fileList)
  input.value = ''
  processFolder(files, names, folderName, totalCount)
}

/**
 * 处理拖拽到上传区域的文件或文件夹
 * @param e - 拖拽事件
 * @returns void
 */
function onDrop(e: DragEvent): void {
  const dt = e.dataTransfer
  if (!dt || !dt.files || dt.files.length === 0) return
  const items = dt.items
  if (items) {
    const fileList: File[] = []
    const traverse = (entry: FileSystemEntry, rootName: string) => {
      if (entry.isFile) {
        (entry as FileSystemFileEntry).file((f) => fileList.push(f))
      } else if (entry.isDirectory) {
        const reader = (entry as FileSystemDirectoryEntry).createReader()
        reader.readEntries((entries) => {
          const dirName = rootName || entry.name
          for (const e of entries) traverse(e, dirName)
        })
      }
    }
    for (let i = 0; i < items.length; i++) {
      const entry = items[i].webkitGetAsEntry?.()
      if (entry) traverse(entry, '')
    }
    setTimeout(() => {
      if (fileList.length === 0) return
      const names = fileList.map((f: any) => f.relativePath || f.webkitRelativePath || f.name)
      const folderName = fileList[0] && (fileList[0] as any).webkitRelativePath
        ? (fileList[0] as any).webkitRelativePath.split('/')[0]
        : ''
      processFolder(fileList, names, folderName, fileList.length)
    }, 200)
    return
  }
  const totalCount = dt.files.length
  const { files, names, folderName } = extractFiles(dt.files)
  processFolder(files, names, folderName, totalCount)
}

/**
 * 调用 Agent 分析文件夹并回填项目元数据
 * @param folderName - 文件夹名称
 * @param fileNames - 文件相对路径列表
 * @returns Promise<void>
 */
async function autoAnalyzeFolder(folderName: string, fileNames: string[]): Promise<void> {
  analyzing.value = true
  try {
    const result = await analyzeFolder({
      folder_name: folderName || '未命名文件夹',
      file_names: fileNames.slice(0, 30),
    })
    form.project_name = result.project_name
    form.description = result.description
    form.language = result.language
    aiFilled.value = true
    ElMessage.success({
      message: `Agent 已识别项目: ${result.project_name}（${result.language_name}）`,
      duration: 4000,
    })
  } catch {
    if (folderName) {
      form.project_name = folderName
      form.description = ''
      form.language = 'plaintext'
      aiFilled.value = true
    }
    ElMessage.warning('Agent 分析失败，请手动填写项目信息')
  } finally {
    analyzing.value = false
  }
}

/**
 * 校验并提交项目表单
 * @returns Promise<void>
 */
async function handleSubmit(): Promise<void> {
  if (!formRef.value) return
  try {
    await formRef.value.validate()
  } catch {
    return
  }
  submitting.value = true
  try {
    const submitData: {
      project_name: string
      description?: string
      language?: string
      files?: File[]
    } = { project_name: form.project_name.trim() }
    if (form.description) submitData.description = form.description.trim()
    if (form.language) submitData.language = form.language
    if (selectedFiles.value.length > 0) submitData.files = selectedFiles.value
    emit('submit', submitData)
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped lang="scss">
.folder-upload-area {
  width: 100%;
}

.folder-drop-zone {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 36px 20px;
  border: 2px dashed var(--color-border-base);
  border-radius: 12px;
  cursor: pointer;
  transition: all 0.2s ease;
  background: var(--gray-50);
  position: relative;
  overflow: hidden;

  &:hover {
    border-color: var(--brand-300);
    background: linear-gradient(135deg, var(--brand-50), #fff);
  }

  &.has-files {
    border-color: var(--brand-300);
    border-style: solid;
    background: var(--brand-50);
    padding: 20px;
    gap: 8px;
  }

  &.analyzing {
    pointer-events: none;
    border-color: var(--brand-400);
    background: linear-gradient(135deg, var(--brand-50), #f0eeff);
  }
}

.folder-icon {
  font-size: 40px;
  color: var(--color-text-placeholder);
  &.filled { color: var(--brand-500); }
  &.is-spinning {
    color: var(--brand-500);
    animation: spin-pulse 1.2s ease-in-out infinite;
  }
}

@keyframes spin-pulse {
  0%, 100% { transform: rotate(0deg) scale(1); }
  50% { transform: rotate(180deg) scale(1.1); }
}

.folder-text {
  margin: 0;
  font-size: 14px;
  color: var(--color-text-regular);
  font-weight: 500;
  b { color: var(--brand-600); }
}

.folder-hint {
  margin: 0;
  font-size: 12px;
  color: var(--color-text-placeholder);
  text-align: center;
  line-height: 1.5;
}

.file-path-box {
  padding: 4px 14px;
  background: rgba(91, 88, 232, 0.06);
  border: 1px solid var(--brand-100);
  border-radius: 6px;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-path-label {
  font-size: 12px;
  color: var(--brand-600);
}

.ai-banner {
  margin-bottom: 12px;
}

.lang-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.lang-badge {
  font-size: 14px;
  font-weight: 500;
  border: none;
}

.field-tag {
  margin-right: 6px;
  transform: translateY(-1px);
}

.text-muted {
  font-size: 13px;
  color: var(--color-text-placeholder);
}
</style>
