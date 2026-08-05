<template>
  <div class="project-detail" v-loading="loading">
    <div class="page-header">
      <el-page-header @back="goBack(router, '/projects')">
        <template #content>
          <span class="header-title">{{ project?.project_name || '项目详情' }}</span>
        </template>
        <template #extra>
          <el-button
            v-if="project && (project.file_count ?? 0) > 0"
            type="danger"
            plain
            :icon="Lock"
            @click="openSecurityScan"
          >
            🛡 安全审计
          </el-button>
          <el-button
            v-if="project && (project.file_count ?? 0) > 0"
            plain
            :icon="Download"
            :loading="downloadingSource"
            @click="handleDownloadSource"
          >
            下载源码
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
      :initial-result="persistedAuditResult"
      @completed="fetchDetail"
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
                <el-button
                  v-if="project.file_count > 0"
                  size="small"
                  plain
                  :icon="Download"
                  :loading="downloadingSource"
                  @click="handleDownloadSource"
                >下载源码</el-button>
                <el-button
                  v-if="project.source_mode !== 'audit_archive'"
                  size="small"
                  @click="handleUploadFile"
                >上传文件</el-button>
                <el-button
                  v-if="project.source_mode !== 'audit_archive'"
                  type="primary"
                  size="small"
                  @click="handleUploadFolder"
                >上传文件夹</el-button>
                <el-button
                  v-if="project.file_count === 0 && project.source_mode !== 'audit_archive'"
                  type="warning"
                  plain
                  size="small"
                  :icon="Lock"
                  :loading="uploadingAudit"
                  @click="auditArchiveInputRef?.click()"
                >上传审计包</el-button>
              </div>
            </div>
            <div v-if="project.source_archive" class="source-archive-band">
              <el-alert
                :title="archiveStatusTitle(project.source_archive.malware_status)"
                :type="project.source_archive.malware_status === 'clean' ? 'success' : 'warning'"
                :closable="false"
                show-icon
              />
              <el-descriptions :column="2" border size="small">
                <el-descriptions-item label="归档">{{ project.source_archive.original_filename }}</el-descriptions-item>
                <el-descriptions-item label="文件">{{ project.source_archive.file_count }}</el-descriptions-item>
                <el-descriptions-item label="压缩 / 解压">
                  {{ formatBytes(project.source_archive.compressed_size) }} /
                  {{ formatBytes(project.source_archive.expanded_size) }}
                </el-descriptions-item>
                <el-descriptions-item label="最大压缩比">
                  {{ project.source_archive.max_compression_ratio.toFixed(1) }}x
                </el-descriptions-item>
                <el-descriptions-item label="威胁命中">
                  {{ project.source_archive.threat_count }}
                </el-descriptions-item>
                <el-descriptions-item label="审计状态">
                  {{ archiveAuditLabel(project.source_archive.audit_status) }}
                </el-descriptions-item>
                <el-descriptions-item label="SHA-256" :span="2">
                  <span class="font-mono archive-sha">{{ project.source_archive.archive_sha256 }}</span>
                </el-descriptions-item>
              </el-descriptions>
            </div>
            <CodeFileList
              v-else
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
    <input
      ref="auditArchiveInputRef"
      type="file"
      :accept="archiveAcceptTypes"
      style="display: none"
      @change="onAuditArchiveSelected"
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
import { goBack } from '@/utils/navigation'

import { Download, Lock, MagicStick, Plus } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import {
  getAuditSourceArchiveResult,
  getProjectDetail,
  downloadProjectSource,
  uploadAuditSourceArchive,
} from '@/api/project'
import { upload, uploadFolder } from '@/api/codeFile'
import {
  listProjectMembers,
  addProjectMember,
  updateProjectMemberRole,
  removeProjectMember,
} from '@/api/projectMember'
import type { ProjectDetailOut } from '@/types/project'
import type { SecurityScanOut } from '@/types/security'
import type {
  ProjectMemberOut,
  ProjectRole,
} from '@/types/projectMember'
import CodeFileList from '@/views/code/CodeFileList.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import AiPromptModal from '@/components/issue/AiPromptModal.vue'
import SecurityScanModal from '@/components/security/SecurityScanModal.vue'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { ElMessage } from 'element-plus/es/components/message/index'

const route = useRoute()
const router = useRouter()

const projectId = Number(route.params.id)
const loading = ref(false)
const project = ref<ProjectDetailOut | null>(null)
const fileListKey = ref(0)
const aiPromptVisible = ref(false)
const securityScanVisible = ref(false)
const persistedAuditResult = ref<SecurityScanOut | null>(null)
const fileInputRef = ref<HTMLInputElement>()
const folderInputRef = ref<HTMLInputElement>()
const auditArchiveInputRef = ref<HTMLInputElement>()
const uploading = ref(false)
const uploadingAudit = ref(false)
const downloadingSource = ref(false)
const archiveExtensions = [
  '.zip', '.7z', '.rar', '.tar', '.gz', '.tgz', '.bz2', '.tbz2', '.xz', '.txz',
  '.zst', '.tzst', '.lz', '.lzma', '.lzip', '.z', '.cpio', '.cab', '.ar', '.xar',
  '.lha', '.lzh', '.iso',
]
const archiveAcceptTypes = archiveExtensions.join(',')
const acceptFileTypes = [
  '.py', '.js', '.ts', '.jsx', '.tsx', '.vue', '.java', '.go', '.c', '.cpp', '.h',
  '.hpp', '.css', '.html', '.json', '.yaml', '.yml', '.xml', ...archiveExtensions,
].join(',')

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`
  return `${(value / 1024 / 1024).toFixed(1)} MiB`
}

function archiveStatusTitle(status: string): string {
  const labels: Record<string, string> = {
    clean: '整包规则扫描未命中恶意特征',
    infected: '已命中恶意特征，源码包保持隔离',
    degraded: '扫描引擎降级，源码包保持隔离',
    error: '扫描引擎异常，源码包保持隔离',
  }
  return labels[status] || '源码包保持隔离'
}

function archiveAuditLabel(status: string): string {
  const labels: Record<string, string> = {
    not_started: '未开始',
    queued: '排队中',
    running: '审计中',
    succeeded: '已完成',
    failed: '失败',
    blocked: '已阻断',
    cancelled: '已取消',
  }
  return labels[status] || status
}

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

async function openSecurityScan(): Promise<void> {
  persistedAuditResult.value = null
  if (project.value?.source_archive?.audit_status === 'succeeded') {
    try {
      const stored = await getAuditSourceArchiveResult(projectId)
      persistedAuditResult.value = stored?.result ?? null
    } catch {
      /* 权限或网络错误已由 http 拦截器提示；仍允许用户打开重新扫描。 */
    }
  }
  securityScanVisible.value = true
}

async function handleDownloadSource(): Promise<void> {
  if (downloadingSource.value) return
  downloadingSource.value = true
  try {
    const blob = await downloadProjectSource(projectId)
    const href = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = href
    link.download = `${project.value?.project_name || `project_${projectId}`}.zip`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(href)
    ElMessage.success('源码归档已开始下载')
  } catch {
    /* http 拦截器已提示错误 */
  } finally {
    downloadingSource.value = false
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
const ARCHIVE_EXTS = new Set(archiveExtensions)

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
  ...archiveExtensions,
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
      const uploaded = await upload(formData)
      if (uploaded.quarantined) {
        ElMessage.warning(`检测到恶意内容,源码包已隔离(仅限沙箱内审计/测试/部署)`)
      } else if (isArchiveFile(files[0].name)) {
        // v2: 压缩包上传后后端自动解压,提示用户解压结果
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

async function onAuditArchiveSelected(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file || uploadingAudit.value) return
  if (!isArchiveFile(file.name)) {
    ElMessage.warning('请选择受支持的源码归档')
    input.value = ''
    return
  }
  uploadingAudit.value = true
  ElMessage.info('正在执行整包结构校验与全成员 YARA 扫描')
  try {
    const result = await uploadAuditSourceArchive(projectId, file)
    if (result.malware_status === 'clean') {
      ElMessage.success(`审计包已接收，覆盖 ${result.file_count} 个文件`)
    } else {
      ElMessage.warning(`审计包已隔离，命中 ${result.threat_count} 个威胁`)
    }
    await fetchDetail()
  } catch {
    /* http 拦截器已提示错误 */
  } finally {
    input.value = ''
    uploadingAudit.value = false
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
  flex-wrap: wrap;
  gap: 8px;
}

.source-archive-band {
  display: grid;
  gap: 12px;
}

.archive-sha {
  display: inline-block;
  max-width: 100%;
  overflow-wrap: anywhere;
}

@media (max-width: 720px) {
  .project-detail {
    padding: 16px;
  }

  .section-header {
    align-items: flex-start;
    flex-direction: column;
    gap: 12px;
  }
}

.text-muted {
  color: var(--el-text-color-placeholder);
}
</style>
