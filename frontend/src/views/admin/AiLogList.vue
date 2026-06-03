<template>
  <div class="ai-log-list-page">
    <div class="page-header">
      <h2>AI调用日志</h2>
    </div>

    <el-card shadow="hover">
      <div class="filter-bar">
        <el-select v-model="filterStatus" placeholder="调用状态" clearable style="width: 120px" @change="loadData">
          <el-option label="成功" value="success" />
          <el-option label="失败" value="failed" />
        </el-select>
        <el-date-picker
          v-model="dateRange"
          type="daterange"
          range-separator="至"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          value-format="YYYY-MM-DD"
          style="width: 260px"
          @change="loadData"
        />
      </div>

      <el-table :data="logs" v-loading="loading" style="width: 100%" @row-click="onRowClick" highlight-current-row>
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column label="项目" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">
            <el-button v-if="row.project_id" link type="primary" @click.stop="goProject(row.project_id)">
              {{ row.project_name || `项目 #${row.project_id}` }}
            </el-button>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="任务" min-width="140" show-overflow-tooltip>
          <template #default="{ row }">
            <el-button v-if="row.task_id" link type="primary" @click.stop="goTask(row.task_id)">
              {{ row.task_name || `任务 #${row.task_id}` }}
            </el-button>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="文件" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">
            <el-button
              v-if="row.project_id && row.file_id"
              link
              type="primary"
              @click.stop="goFile(row.project_id, row.file_id)"
            >
              {{ row.file_name || `文件 #${row.file_id}` }}
            </el-button>
            <span v-else>{{ row.file_name || (row.file_id ? `文件 #${row.file_id}` : '-') }}</span>
          </template>
        </el-table-column>
        <el-table-column label="用户" width="120" show-overflow-tooltip>
          <template #default="{ row }">
            {{ row.user_name || (row.user_id ? `#${row.user_id}` : '-') }}
          </template>
        </el-table-column>
        <el-table-column prop="chunk_index" label="分片" width="70">
          <template #default="{ row }">{{ row.chunk_index ?? '-' }}</template>
        </el-table-column>
        <el-table-column prop="model_name" label="模型" width="140" show-overflow-tooltip />
        <el-table-column prop="prompt_tokens" label="输入Token" width="100" sortable />
        <el-table-column prop="completion_tokens" label="输出Token" width="100" sortable />
        <el-table-column prop="total_tokens" label="总Token" width="100" sortable />
        <el-table-column prop="duration_ms" label="耗时" width="100" sortable>
          <template #default="{ row }">
            {{ formatDuration(row.duration_ms) }}
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.status === 'success' ? 'success' : 'danger'" size="small">
              {{ row.status === 'success' ? '成功' : '失败' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="create_time" label="调用时间" width="170" sortable>
          <template #default="{ row }">
            {{ formatDateTime(row.create_time) }}
          </template>
        </el-table-column>
        <el-table-column label="追溯" width="150" fixed="right">
          <template #default="{ row }">
            <el-button v-if="row.task_id" link type="primary" @click.stop="goTask(row.task_id)">任务</el-button>
            <el-button
              v-if="row.project_id && row.file_id"
              link
              type="primary"
              @click.stop="goFile(row.project_id, row.file_id)"
            >
              文件
            </el-button>
            <span v-if="!row.task_id && !(row.project_id && row.file_id)" class="text-muted">仅日志</span>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination-wrapper">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          @change="loadData"
        />
      </div>
    </el-card>

    <el-dialog v-model="detailVisible" title="日志详情" width="700px">
      <div v-if="detail" class="log-detail">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="日志ID">{{ detail.id }}</el-descriptions-item>
          <el-descriptions-item label="项目">
            <el-button v-if="detail.project_id" link type="primary" @click="goProject(detail.project_id)">
              {{ detail.project_name || `项目 #${detail.project_id}` }}
            </el-button>
            <span v-else>-</span>
          </el-descriptions-item>
          <el-descriptions-item label="任务">
            <el-button v-if="detail.task_id" link type="primary" @click="goTask(detail.task_id)">
              {{ detail.task_name || `任务 #${detail.task_id}` }}
            </el-button>
            <span v-else>-</span>
          </el-descriptions-item>
          <el-descriptions-item label="文件">
            <el-button
              v-if="detail.project_id && detail.file_id"
              link
              type="primary"
              @click="goFile(detail.project_id, detail.file_id)"
            >
              {{ detail.file_name || `文件 #${detail.file_id}` }}
            </el-button>
            <span v-else>{{ detail.file_name || (detail.file_id ? `文件 #${detail.file_id}` : '-') }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="用户">
            {{ detail.user_name || (detail.user_id ? `#${detail.user_id}` : '-') }}
          </el-descriptions-item>
          <el-descriptions-item label="分片">{{ detail.chunk_index ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="模型">{{ detail.model_name }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="detail.status === 'success' ? 'success' : 'danger'" size="small">
              {{ detail.status === 'success' ? '成功' : '失败' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="输入Token">{{ detail.prompt_tokens ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="输出Token">{{ detail.completion_tokens ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="总Token">{{ detail.total_tokens ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="耗时">{{ formatDuration(detail.duration_ms) }}</el-descriptions-item>
          <el-descriptions-item label="调用时间">{{ formatDateTime(detail.create_time) }}</el-descriptions-item>
        </el-descriptions>

        <div v-if="detail.error_message" class="detail-section">
          <div class="detail-label">错误信息</div>
          <div class="detail-content error-content">{{ detail.error_message }}</div>
        </div>

        <div v-if="detail.prompt" class="detail-section">
          <div class="detail-label">
            <span>请求Prompt</span>
            <el-button link type="primary" size="small" @click="copyText(detail.prompt)">复制</el-button>
          </div>
          <div class="detail-code">
            <pre><code>{{ detail.prompt }}</code></pre>
          </div>
        </div>

        <div v-if="detail.response" class="detail-section">
          <div class="detail-label">
            <span>AI响应</span>
            <el-button link type="primary" size="small" @click="copyText(detail.response)">复制</el-button>
          </div>
          <div class="detail-code">
            <pre><code>{{ detail.response }}</code></pre>
          </div>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { getAiLogs, getAiLogDetail } from '@/api/aiLog'
import type { AiLogOut, AiLogDetailOut } from '@/types/aiLog'
import { formatDateTime } from '@/utils/format'

const router = useRouter()
const loading = ref(false)
const logs = ref<AiLogOut[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filterStatus = ref('')
const dateRange = ref<[string, string] | null>(null)

const detailVisible = ref(false)
const detail = ref<AiLogDetailOut | null>(null)

function formatDuration(ms?: number): string {
  if (!ms) return '-'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60000)}m${Math.round((ms % 60000) / 1000)}s`
}

async function loadData() {
  loading.value = true
  try {
    const params: Record<string, unknown> = {
      page: page.value,
      page_size: pageSize.value,
    }
    if (filterStatus.value) params.status = filterStatus.value
    if (dateRange.value) {
      params.start = dateRange.value[0]
      params.end = dateRange.value[1]
    }

    const data = await getAiLogs(params)
    logs.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

async function onRowClick(row: AiLogOut) {
  try {
    detail.value = await getAiLogDetail(row.id)
    detailVisible.value = true
  } catch {
    ElMessage.error('获取日志详情失败')
  }
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制到剪贴板')
  } catch {
    ElMessage.error('复制失败')
  }
}

/**
 * 跳转到项目详情页。
 * @param projectId - 项目 ID
 * @returns void
 */
function goProject(projectId: number): void {
  router.push(`/projects/${projectId}`)
}

/**
 * 跳转到审查任务详情页。
 * @param taskId - 审查任务 ID
 * @returns void
 */
function goTask(taskId: number): void {
  router.push(`/reviews/${taskId}`)
}

/**
 * 跳转到代码文件编辑页。
 * @param projectId - 文件所属项目 ID
 * @param fileId - 代码文件 ID
 * @returns void
 */
function goFile(projectId: number, fileId: number): void {
  router.push(`/code/${projectId}/file/${fileId}`)
}

onMounted(() => {
  loadData()
})
</script>

<style scoped lang="scss">
.ai-log-list-page {
  .page-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 20px;

    h2 {
      margin: 0;
      font-size: 20px;
      font-weight: 600;
    }
  }
}

.filter-bar {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}

.pagination-wrapper {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}

.text-muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.log-detail {
  .detail-section {
    margin-top: 16px;
  }

  .detail-label {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 14px;
    font-weight: 600;
    color: var(--el-text-color-primary);
    margin-bottom: 8px;
  }

  .detail-content {
    font-size: 13px;
    line-height: 1.8;
    color: var(--el-text-color-regular);
    padding: 8px 12px;
    background: var(--el-fill-color-light);
    border-radius: 4px;

    &.error-content {
      color: #f56c6c;
    }
  }

  .detail-code {
    background: #1e1e1e;
    border-radius: 4px;
    padding: 12px;
    max-height: 350px;
    overflow: auto;

    pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-all;

      code {
        color: #d4d4d4;
        font-size: 12px;
        line-height: 1.6;
        font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
      }
    }
  }
}
</style>
