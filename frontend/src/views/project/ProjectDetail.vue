<template>
  <div class="project-detail" v-loading="loading">
    <div class="page-header">
      <el-page-header @back="router.back()">
        <template #content>
          <span class="header-title">{{ project?.project_name || '项目详情' }}</span>
        </template>
        <template #extra>
          <el-button
            v-if="project && (project.file_count ?? 0) > 0"
            type="danger"
            plain
            :icon="Lock"
            @click="securityScanVisible = true"
          >
            🛡 安全审计
          </el-button>
          <el-button
            v-if="project && (project.file_count ?? 0) > 0"
            type="primary"
            plain
            :icon="MagicStick"
            @click="aiPromptVisible = true"
          >
            AI 修复手册
          </el-button>
        </template>
      </el-page-header>
    </div>

    <AiPromptModal
      v-model="aiPromptVisible"
      source="project"
      :ref-id="projectId"
    />

    <SecurityScanModal
      v-model="securityScanVisible"
      source="project"
      :ref-id="projectId"
      :ref-name="project?.project_name || ''"
    />

    <template v-if="project">
      <el-descriptions :column="3" border class="info-card">
        <el-descriptions-item label="项目名称">{{ project.project_name }}</el-descriptions-item>
        <el-descriptions-item label="编程语言">
          <el-tag v-if="project.language" size="small" type="info">{{ project.language }}</el-tag>
          <span v-else class="text-muted">未设置</span>
        </el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag :type="project.status === 'active' ? 'success' : 'info'" size="small">
            {{ project.status === 'active' ? '活跃' : '归档' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="文件数量">{{ project.file_count }}</el-descriptions-item>
        <el-descriptions-item label="创建时间">{{ formatDate(project.create_time) }}</el-descriptions-item>
        <el-descriptions-item label="更新时间">{{ formatDate(project.update_time) }}</el-descriptions-item>
        <el-descriptions-item v-if="project.description" label="描述" :span="3">
          {{ project.description }}
        </el-descriptions-item>
      </el-descriptions>

      <div class="section">
        <div class="section-header">
          <h3>代码文件</h3>
          <div class="section-actions">
            <el-button size="small" @click="handleUploadFile">上传文件</el-button>
            <el-button type="primary" size="small" @click="handleUploadFolder">上传文件夹</el-button>
          </div>
        </div>
        <CodeFileList
          :project-id="projectId"
          :key="fileListKey"
          @uploaded="onFileUploaded"
        />
      </div>

      <div class="section">
        <div class="section-header">
          <h3>最近审查任务</h3>
        </div>
        <el-table
          v-if="project.recent_tasks.length > 0"
          :data="project.recent_tasks"
          border
          stripe
          empty-text="暂无审查记录"
        >
          <el-table-column label="任务编号" width="100" align="center">
            <template #default="{ row }">
              {{ row.id }}
            </template>
          </el-table-column>
          <el-table-column label="评分" width="100" align="center">
            <template #default="{ row }">
              <el-tag :type="getScoreType(row.score)" size="small">{{ row.score }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="问题数" width="100" align="center">
            <template #default="{ row }">
              {{ row.total_issues }}
            </template>
          </el-table-column>
          <el-table-column label="状态" width="120" align="center">
            <template #default="{ row }">
              <el-tag :type="getStatusType(row.status)" size="small">
                {{ statusLabels[row.status] ?? row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="创建时间" min-width="180" align="center">
            <template #default="{ row }">
              {{ formatDate(row.create_time) }}
            </template>
          </el-table-column>
        </el-table>
        <EmptyState v-else description="暂无审查记录" />
      </div>
    </template>

    <EmptyState v-else-if="!loading" description="项目不存在" />

    <input
      ref="fileInputRef"
      type="file"
      :accept="acceptFileTypes"
      style="display: none"
      @change="onFileSelected"
    />
    <input
      ref="folderInputRef"
      type="file"
      webkitdirectory
      directory
      multiple
      style="display: none"
      @change="onFolderSelected"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Lock, MagicStick } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import { getProjectDetail } from '@/api/project'
import { upload, uploadFolder } from '@/api/codeFile'
import type { ProjectDetailOut } from '@/types/project'
import CodeFileList from '@/views/code/CodeFileList.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import AiPromptModal from '@/components/issue/AiPromptModal.vue'
import SecurityScanModal from '@/components/security/SecurityScanModal.vue'

const route = useRoute()
const router = useRouter()

const projectId = Number(route.params.id)
const loading = ref(false)
const project = ref<ProjectDetailOut | null>(null)
const fileListKey = ref(0)
const aiPromptVisible = ref(false)
const securityScanVisible = ref(false)
const fileInputRef = ref<HTMLInputElement>()
const folderInputRef = ref<HTMLInputElement>()
const uploading = ref(false)
const acceptFileTypes = '.py,.js,.ts,.jsx,.tsx,.vue,.java,.go,.c,.cpp,.h,.hpp,.css,.html,.json,.yaml,.yml,.xml'

const statusLabels: Record<string, string> = {
  pending: '待处理',
  running: '运行中',
  success: '成功',
  failed: '失败',
  cancelled: '已取消',
}

function formatDate(dateStr: string): string {
  return dayjs(dateStr).format('YYYY-MM-DD HH:mm')
}

function getScoreType(score: number): 'success' | 'warning' | 'danger' {
  if (score >= 80) return 'success'
  if (score >= 60) return 'warning'
  return 'danger'
}

function getStatusType(status: string): 'success' | 'warning' | 'info' | 'danger' {
  const map: Record<string, 'success' | 'warning' | 'info' | 'danger'> = {
    success: 'success',
    running: 'warning',
    pending: 'info',
    failed: 'danger',
    cancelled: 'info',
  }
  return map[status] || 'info'
}

async function fetchDetail(): Promise<void> {
  loading.value = true
  try {
    project.value = await getProjectDetail(projectId)
  } catch {
    project.value = null
  } finally {
    loading.value = false
  }
}

function handleUploadFile(): void {
  fileInputRef.value?.click()
}

function handleUploadFolder(): void {
  folderInputRef.value?.click()
}

function getExtDetail(filename: string): string {
  const m = filename.match(/\.([a-zA-Z0-9]+)$/)
  return m ? '.' + m[1].toLowerCase() : ''
}

const VALID_EXTS_DETAIL = new Set([
  '.py', '.js', '.ts', '.jsx', '.tsx', '.mjs', '.cjs',
  '.vue', '.svelte', '.java', '.kt', '.go', '.rs', '.rb', '.php',
  '.c', '.cpp', '.cc', '.cxx', '.h', '.hpp', '.hxx',
  '.css', '.scss', '.less', '.sass',
  '.html', '.htm', '.json', '.yaml', '.yml', '.toml', '.xml',
  '.sql', '.sh', '.bash', '.md', '.txt', '.cfg', '.ini',
])

async function onFileSelected(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement
  const fileList = input.files
  if (!fileList || fileList.length === 0) return
  if (uploading.value) return
  uploading.value = true

  const files: File[] = []
  for (let i = 0; i < fileList.length; i++) {
    const f = fileList[i]
    const ext = getExtDetail(f.name)
    if (ext && VALID_EXTS_DETAIL.has(ext)) files.push(f)
  }

  try {
    if (files.length === 1) {
      const formData = new FormData()
      formData.append('file', files[0])
      formData.append('project_id', String(projectId))
      await upload(formData)
      ElMessage.success('文件上传成功')
    } else if (files.length > 1) {
      const result = await uploadFolder(projectId, files)
      if (result.success_count > 0) {
        ElMessage.success(`成功上传 ${result.success_count} 个文件`)
      }
      if (result.fail_count > 0) {
        ElMessage.warning(`${result.fail_count} 个文件上传失败`)
      }
    }
    fileListKey.value++
    fetchDetail()
  } catch {
    /* handled by interceptor */
  } finally {
    input.value = ''
    uploading.value = false
  }
}

async function onFolderSelected(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement
  const fileList = input.files
  if (!fileList || fileList.length === 0) return
  if (uploading.value) return
  uploading.value = true

  const files: File[] = []
  for (let i = 0; i < fileList.length; i++) {
    const f = fileList[i]
    const ext = getExtDetail(f.name)
    if (ext && VALID_EXTS_DETAIL.has(ext)) files.push(f)
  }

  try {
    if (files.length > 0) {
      ElMessage.info(`正在上传 ${files.length} 个文件...`)
      const result = await uploadFolder(projectId, files)
      if (result.success_count > 0) {
        ElMessage.success(`成功上传 ${result.success_count} 个文件`)
      }
      if (result.fail_count > 0) {
        ElMessage.warning(`${result.fail_count} 个文件上传失败`)
      }
    } else {
      ElMessage.warning('未找到支持的代码文件')
    }
    fileListKey.value++
    fetchDetail()
  } catch {
    /* handled by interceptor */
  } finally {
    input.value = ''
    uploading.value = false
  }
}

function onFileUploaded(): void {
  fileListKey.value++
}

onMounted(() => {
  fetchDetail()
})
</script>

<style scoped lang="scss">
.project-detail {
  padding: 24px;
}

.page-header {
  margin-bottom: 20px;
}

.header-title {
  font-size: 18px;
  font-weight: 600;
}

.info-card {
  margin-bottom: 24px;
}

.section {
  margin-bottom: 24px;
}

.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;

  h3 {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
  }
}

.section-actions {
  display: flex;
  gap: 8px;
}

.text-muted {
  color: var(--el-text-color-placeholder);
}
</style>
