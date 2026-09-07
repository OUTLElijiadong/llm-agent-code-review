<template>
  <div class="version-history-page">
    <div class="page-header">
      <el-page-header @back="goBack(router, '/code')">
        <template #content>
          <span class="header-title">版本历史</span>
        </template>
      </el-page-header>
    </div>

    <el-table
      v-loading="loading"
      :data="versions"
      border
      stripe
      empty-text="暂无版本记录"
    >
      <el-table-column label="版本号" width="100" align="center">
        <template #default="{ row }">
          <el-tag size="small" type="primary">v{{ row.version_no }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="change_desc" label="变更描述" min-width="240">
        <template #default="{ row }">
          <span v-if="row.change_desc">{{ row.change_desc }}</span>
          <span v-else class="text-muted">无描述</span>
        </template>
      </el-table-column>
      <el-table-column prop="create_time" label="创建时间" width="200" align="center">
        <template #default="{ row }">
          {{ formatDate(row.create_time) }}
        </template>
      </el-table-column>
      <el-table-column label="操作" width="180" align="center" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="handleView(row.version_no)">查看</el-button>
          <el-popconfirm
            v-if="canRestore"
            title="确定恢复到此版本吗？当前内容将被覆盖"
            confirm-button-text="确定"
            cancel-button-text="取消"
            @confirm="handleRestore(row.version_no)"
          >
            <template #reference>
              <el-button link type="warning" :loading="restoring">恢复</el-button>
            </template>
          </el-popconfirm>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog
      v-model="contentDialogVisible"
      title="版本内容"
      width="70%"
      :close-on-click-modal="false"
      destroy-on-close
    >
      <div v-loading="contentLoading" class="version-content-wrap">
        <template v-if="versionContent">
          <el-alert
            v-if="versionDetail?.change_desc"
            :title="`变更描述: ${versionDetail.change_desc}`"
            type="info"
            :closable="false"
            class="version-alert"
          />
          <el-input
            :model-value="versionContent"
            type="textarea"
            :rows="20"
            readonly
            class="content-textarea"
          />
        </template>
        <EmptyState v-else-if="!contentLoading" description="无法加载版本内容" />
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
/**
 * 版本历史页�?
 * 展示文件版本历史列表，支持查看和恢复版本
 */
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { goBack } from '@/utils/navigation'

import dayjs from 'dayjs'
import EmptyState from '@/components/common/EmptyState.vue'
import { useUserStore } from '@/stores/user'
import { getProjectDetail } from '@/api/project'
import { getDetail, listVersions, getVersion, restoreVersion } from '@/api/codeFile'
import type { VersionOut, VersionDetailOut } from '@/types/project'
import { ElMessage } from 'element-plus/es/components/message/index'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const projectWritable = ref(false)
const canView = computed(() => userStore.hasPermission('file:view'))
const canRestore = computed(() => projectWritable.value && userStore.hasPermission('file:edit'))
const restoring = ref(false)

const fileId = Number(route.params.fileId)

const loading = ref(false)
const versions = ref<VersionOut[]>([])

const contentDialogVisible = ref(false)
const contentLoading = ref(false)
const versionContent = ref('')
const versionDetail = ref<VersionDetailOut | null>(null)

/**
 * 格式化日期
 * @param dateStr - 日期字符串
 * @returns 格式化后的日期字符�?
 */
function formatDate(dateStr: string): string {
  return dayjs(dateStr).format('YYYY-MM-DD HH:mm:ss')
}

/**
 * 获取版本列表
 */
async function fetchVersions(): Promise<void> {
  if (!canView.value) return
  loading.value = true
  try {
    const res = await listVersions(fileId)
    versions.value = res.items
  } catch {
    versions.value = []
  } finally {
    loading.value = false
  }
}

/**
 * 查看版本内容
 * @param versionNo - 版本号
 */
async function handleView(versionNo: number): Promise<void> {
  if (!canView.value) return
  contentDialogVisible.value = true
  contentLoading.value = true
  versionContent.value = ''
  versionDetail.value = null

  try {
    const detail = await getVersion(fileId, versionNo)
    versionDetail.value = detail
    versionContent.value = detail.content
  } catch {
    versionContent.value = ''
  } finally {
    contentLoading.value = false
  }
}

/**
 * 恢复到指定版本
 * @param versionNo - 版本号
 */
async function handleRestore(versionNo: number): Promise<void> {
  if (!canRestore.value || restoring.value) return
  restoring.value = true
  try {
    await restoreVersion(fileId, versionNo)
    ElMessage.success('版本恢复成功')
    await fetchVersions()
  } catch {
    // 错误已在拦截器处理
  } finally {
    restoring.value = false
  }
}

async function fetchProjectPermission(): Promise<void> {
  projectWritable.value = false
  if (!canView.value || !userStore.hasPermission('file:edit') || !userStore.hasPermission('project:view')) return
  try {
    const file = await getDetail(fileId)
    const project = await getProjectDetail(file.project_id)
    projectWritable.value = project.id === file.project_id && project.can_update
  } catch { /* 资源授权未确认时不提供恢复操作。 */ }
}

onMounted(() => {
  void fetchVersions()
  void fetchProjectPermission()
})
</script>

<style scoped lang="scss">
.version-history-page {
  padding: 24px;
}

.page-header {
  margin-bottom: 20px;
}

.header-title {
  font-size: 16px;
  font-weight: 600;
}

.text-muted {
  color: var(--el-text-color-placeholder);
}

.version-content-wrap {
  min-height: 300px;
}

.version-alert {
  margin-bottom: 12px;
}

.content-textarea {
  :deep(.el-textarea__inner) {
    font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
    font-size: 13px;
    line-height: 1.5;
  }
}
</style>
