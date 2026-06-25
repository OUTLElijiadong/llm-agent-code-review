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
      <el-tabs v-model="activeTab" class="detail-tabs">
        <!-- Tab 1: 项目信息 -->
        <el-tab-pane label="项目信息" name="info">
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
        </el-tab-pane>

        <!-- Tab 2: 代码文件 -->
        <el-tab-pane label="代码文件" name="files">
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
        </el-tab-pane>

        <!-- Tab 3: 审查任务 -->
        <el-tab-pane label="审查任务" name="tasks">
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
        </el-tab-pane>

        <!-- Tab 4: 成员管理 -->
        <el-tab-pane label="成员管理" name="members">
          <div class="section">
            <div class="section-header">
              <h3>项目成员</h3>
              <div class="section-actions">
                <el-button
                  type="primary"
                  size="small"
                  :icon="Plus"
                  @click="openAddMemberDialog"
                >
                  添加成员
                </el-button>
              </div>
            </div>
            <el-table
              v-loading="memberLoading"
              :data="members"
              border
              stripe
              empty-text="暂无成员"
            >
              <el-table-column label="用户名" min-width="140">
                <template #default="{ row }">
                  {{ row.username }}
                </template>
              </el-table-column>
              <el-table-column label="昵称" min-width="140">
                <template #default="{ row }">
                  {{ row.nickname || '—' }}
                </template>
              </el-table-column>
              <el-table-column label="项目角色" width="140" align="center">
                <template #default="{ row }">
                  <el-tag :type="row.role_in_project === 'owner' ? 'warning' : 'info'" size="small">
                    {{ row.role_in_project === 'owner' ? '负责人' : '审查员' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="加入时间" width="180" align="center">
                <template #default="{ row }">
                  {{ formatDate(row.create_time) }}
                </template>
              </el-table-column>
              <el-table-column label="操作" width="200" align="center">
                <template #default="{ row }">
                  <el-select
                    v-if="row.role_in_project !== 'owner'"
                    :model-value="row.role_in_project"
                    size="small"
                    style="width: 100px; margin-right: 8px"
                    @change="(val: ProjectRole) => handleChangeRole(row.user_id, val)"
                  >
                    <el-option label="审查员" value="reviewer" />
                    <el-option label="负责人" value="owner" />
                  </el-select>
                  <el-button
                    v-if="row.role_in_project !== 'owner'"
                    type="danger"
                    size="small"
                    link
                    @click="handleRemoveMember(row)"
                  >
                    移除
                  </el-button>
                  <span v-else class="text-muted">—</span>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-tab-pane>
      </el-tabs>
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

    <!-- 添加成员对话框 -->
    <el-dialog
      v-model="addMemberVisible"
      title="添加项目成员"
      width="440px"
      @closed="resetAddForm"
    >
      <el-form :model="addForm" label-width="90px">
        <el-form-item label="用户 ID">
          <el-input-number
            v-model="addForm.user_id"
            :min="1"
            :controls="false"
            style="width: 100%"
            placeholder="请输入被添加用户的 ID"
          />
        </el-form-item>
        <el-form-item label="项目角色">
          <el-select v-model="addForm.role_in_project" style="width: 100%">
            <el-option label="审查员" value="reviewer" />
            <el-option label="负责人" value="owner" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addMemberVisible = false">取消</el-button>
        <el-button type="primary" :loading="addSubmitting" @click="submitAddMember">
          确认添加
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Lock, MagicStick, Plus } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import { getProjectDetail } from '@/api/project'
import { upload, uploadFolder } from '@/api/codeFile'
import {
  listProjectMembers,
  addProjectMember,
  updateProjectMemberRole,
  removeProjectMember,
} from '@/api/projectMember'
import type { ProjectDetailOut } from '@/types/project'
import type {
  ProjectMemberOut,
  ProjectRole,
} from '@/types/projectMember'
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
const acceptFileTypes = '.py,.js,.ts,.jsx,.tsx,.vue,.java,.go,.c,.cpp,.h,.hpp,.css,.html,.json,.yaml,.yml,.xml,.zip,.tar,.gz,.tgz,.bz2,.xz'

// ── Tab 切换 ──
const activeTab = ref<'info' | 'files' | 'tasks' | 'members'>('info')

// ── 成员管理状态 ──
const members = ref<ProjectMemberOut[]>([])
const memberLoading = ref(false)
const addMemberVisible = ref(false)
const addSubmitting = ref(false)
const addForm = ref<{ user_id: number | null; role_in_project: ProjectRole }>({
  user_id: null,
  role_in_project: 'reviewer',
})

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

/** v2: 压缩包扩展名集合,后端将自动解压并批量创建文件 */
const ARCHIVE_EXTS = new Set(['.zip', '.tar', '.gz', '.tgz', '.bz2', '.xz'])

/**
 * 判断文件是否为压缩包
 * @param filename - 文件名
 * @returns 是否压缩包
 */
function isArchiveFile(filename: string): boolean {
  return ARCHIVE_EXTS.has(getExtDetail(filename))
}

const VALID_EXTS_DETAIL = new Set([
  '.py', '.js', '.ts', '.jsx', '.tsx', '.mjs', '.cjs',
  '.vue', '.svelte', '.java', '.kt', '.go', '.rs', '.rb', '.php',
  '.c', '.cpp', '.cc', '.cxx', '.h', '.hpp', '.hxx',
  '.css', '.scss', '.less', '.sass',
  '.html', '.htm', '.json', '.yaml', '.yml', '.toml', '.xml',
  '.sql', '.sh', '.bash', '.md', '.txt', '.cfg', '.ini',
  // v2: 压缩包扩展名,后端自动解压
  '.zip', '.tar', '.gz', '.tgz', '.bz2', '.xz',
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
      // v2: 压缩包上传后后端自动解压,提示用户解压结果
      if (isArchiveFile(files[0].name)) {
        ElMessage.success(`压缩包 ${files[0].name} 已上传并自动解压,请刷新文件列表查看`)
      } else {
        ElMessage.success('文件上传成功')
      }
    } else if (files.length > 1) {
      // v2: 多文件场景分离压缩包与普通文件
      const archiveFiles = files.filter(f => isArchiveFile(f.name))
      const normalFiles = files.filter(f => !isArchiveFile(f.name))
      let successCount = 0
      let failCount = 0
      // 普通文件走批量上传接口
      if (normalFiles.length > 0) {
        const result = await uploadFolder(projectId, normalFiles)
        successCount += result.success_count
        failCount += result.fail_count
      }
      // 压缩包逐个上传(后端自动解压)
      for (const af of archiveFiles) {
        try {
          const formData = new FormData()
          formData.append('file', af)
          formData.append('project_id', String(projectId))
          await upload(formData)
          successCount += 1
        } catch {
          failCount += 1
        }
      }
      if (successCount > 0) {
        ElMessage.success(`成功上传 ${successCount} 个文件(含压缩包自动解压)`)
      }
      if (failCount > 0) {
        ElMessage.warning(`${failCount} 个文件上传失败`)
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

// ── 成员管理逻辑 ──

/**
 * 拉取项目成员列表
 */
async function fetchMembers(): Promise<void> {
  memberLoading.value = true
  try {
    members.value = await listProjectMembers(projectId)
  } catch {
    /* 错误已由 http 拦截器提示 */
  } finally {
    memberLoading.value = false
  }
}

/**
 * 打开添加成员对话框
 */
function openAddMemberDialog(): void {
  addMemberVisible.value = true
}

/**
 * 重置添加表单（对话框关闭时调用）
 */
function resetAddForm(): void {
  addForm.value = { user_id: null, role_in_project: 'reviewer' }
}

/**
 * 提交添加成员
 */
async function submitAddMember(): Promise<void> {
  if (!addForm.value.user_id) {
    ElMessage.warning('请输入用户 ID')
    return
  }
  addSubmitting.value = true
  try {
    await addProjectMember(projectId, {
      user_id: addForm.value.user_id,
      role_in_project: addForm.value.role_in_project,
    })
    ElMessage.success('成员添加成功')
    addMemberVisible.value = false
    fetchMembers()
  } catch {
    /* 错误已由 http 拦截器提示 */
  } finally {
    addSubmitting.value = false
  }
}

/**
 * 切换成员项目角色
 * @param userId - 被更新用户的 ID
 * @param newRole - 新角色
 */
async function handleChangeRole(userId: number, newRole: ProjectRole): Promise<void> {
  try {
    await updateProjectMemberRole(projectId, userId, { role_in_project: newRole })
    ElMessage.success('角色已更新')
    fetchMembers()
  } catch {
    /* 错误已由 http 拦截器提示 */
  }
}

/**
 * 移除项目成员（带二次确认）
 * @param row - 成员行数据
 */
async function handleRemoveMember(row: ProjectMemberOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定要将用户「${row.username}」移出项目吗？`,
      '移除成员',
      { type: 'warning', confirmButtonText: '移除', cancelButtonText: '取消' },
    )
  } catch {
    return /* 用户取消 */
  }
  try {
    await removeProjectMember(projectId, row.user_id)
    ElMessage.success('成员已移除')
    fetchMembers()
  } catch {
    /* 错误已由 http 拦截器提示 */
  }
}

onMounted(() => {
  fetchDetail()
  fetchMembers()
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

.detail-tabs {
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
